"""Embedding models via OpenRouter: model registry, client, cache.

`EmbeddingSpec` holds what differs per model and silently degrades retrieval
when wrong: the query/document prefixes, the dimension, the input limit.
`OpenRouterEmbeddings` implements LangChain's `Embeddings` interface on top of
OpenRouter's OpenAI-compatible endpoint (POST /embeddings). It is a small
custom client rather than `langchain_openai.OpenAIEmbeddings` because it must
apply per-model prefixes, adapt the batch size, retry on rate limits but stop
on an exhausted daily free quota, report token usage, and use a disk cache.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import struct
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx
from langchain_core.embeddings import Embeddings

from app.config import OPENROUTER_BASE_URL


@dataclass(frozen=True)
class EmbeddingSpec:
    slug: str  # OpenRouter model id
    dim: int  # output dimension (checked against the API on first call)
    query_prefix: str  # prepended to user queries
    doc_prefix: str  # prepended to chunks at ingestion
    max_tokens: int  # model input limit
    normalize: bool = True  # L2-normalise client-side (cosine in Qdrant either way)
    notes: str = ""


# Free OpenRouter embedding models (the only ":free" ones listed on 2026-11-01).
# Prefixes are quoted from each model's config_sentence_transformers.json / model card;
# sources and caveats in docs/EMBEDDING_CANDIDATES.md.
_NEMOTRON_Q, _NEMOTRON_D = "query: ", "passage: "
REGISTRY: dict[str, EmbeddingSpec] = {
    s.slug: s
    for s in (
        EmbeddingSpec(
            "nvidia/nemotron-3.5-embed-1b:free",
            2048,
            _NEMOTRON_Q,
            _NEMOTRON_D,
            32768,
            notes="multimodal (text + page images); Matryoshka; OpenMDW-1.1",
        ),
        EmbeddingSpec(
            "nvidia/nemotron-3.5-embed-8b:free",
            4096,
            _NEMOTRON_Q,
            _NEMOTRON_D,
            32768,
            notes="multimodal; Matryoshka; OpenMDW-1.1",
        ),
        EmbeddingSpec(
            "nvidia/nemotron-3-embed-1b:free",
            2048,
            _NEMOTRON_Q,
            _NEMOTRON_D,
            32768,
            notes="text-only; Matryoshka; OpenMDW-1.1",
        ),
        EmbeddingSpec(
            "nvidia/llama-nemotron-embed-vl-1b-v2:free",
            2048,
            _NEMOTRON_Q,
            _NEMOTRON_D,
            8192,
            notes="multimodal QA retrieval; card evaluated text up to 8192 tokens; NVIDIA Open "
            "Model License + Llama 3.2",
        ),
        EmbeddingSpec(
            "liquid/lfm-2.5-embedding-350m:free",
            1024,
            "query: ",
            "document: ",
            512,
            notes="document prefix is 'document: ' (not 'passage: '); 512-token limit; "
            "LFM Open License v1.0; requests may be retained for training",
        ),
    )
}


def get_spec(slug: str) -> EmbeddingSpec:
    try:
        return REGISTRY[slug]
    except KeyError:
        known = ", ".join(sorted(REGISTRY))
        raise ValueError(f"Unknown EMBEDDING_MODEL '{slug}'. Known models: {known}") from None


class EmbeddingError(RuntimeError):
    """The embeddings API failed in a way retrying will not fix."""


class QuotaExhausted(EmbeddingError):
    """OpenRouter's daily free-model request quota is used up."""


class EmbeddingCache:
    """Content-hash keyed vector cache in SQLite, so nothing is paid (or rate-limited) twice.

    Thread-safe: Streamlit and LangGraph run nodes on worker threads, so the
    connection is shared across threads and every access is serialised by a lock.
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("CREATE TABLE IF NOT EXISTS emb (key TEXT PRIMARY KEY, vec BLOB NOT NULL)")

    @staticmethod
    def key(model: str, text: str) -> str:
        return hashlib.sha256(f"{model}\x00{text}".encode()).hexdigest()

    def get_many(self, keys: Sequence[str]) -> dict[str, list[float]]:
        # One static, parameterised query: the keys travel as a single JSON array.
        with self._lock:
            rows = self._db.execute(
                "SELECT key, vec FROM emb WHERE key IN (SELECT value FROM json_each(?))",
                (json.dumps(list(keys)),),
            ).fetchall()
        return {k: list(struct.unpack(f"{len(blob) // 4}f", blob)) for k, blob in rows}

    def put_many(self, items: dict[str, list[float]]) -> None:
        rows = [(k, struct.pack(f"{len(v)}f", *v)) for k, v in items.items()]
        with self._lock:
            self._db.executemany("INSERT OR REPLACE INTO emb (key, vec) VALUES (?, ?)", rows)
            self._db.commit()

    def close(self) -> None:
        self._db.close()


def _l2(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


class OpenRouterEmbeddings(Embeddings):
    """LangChain-compatible embeddings for one registry model, via OpenRouter."""

    def __init__(
        self,
        spec: EmbeddingSpec,
        api_key: str,
        *,
        base_url: str = OPENROUTER_BASE_URL,
        batch_size: int = 64,
        timeout: float = 60.0,
        max_retries: int = 5,
        cache: EmbeddingCache | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.spec = spec
        self.batch_size = batch_size
        self.max_retries = max_retries
        self.cache = cache
        self._sleep = sleep
        self._client = httpx.Client(
            base_url=base_url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "X-OpenRouter-Title": "iphone-guide-rag",
            },
            timeout=timeout,
            transport=transport,
        )
        self.requests_made = 0
        self.prompt_tokens = 0

    # --- LangChain interface -------------------------------------------------------------

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed([self.spec.doc_prefix + t for t in texts])

    def embed_query(self, text: str) -> list[float]:
        return self._embed([self.spec.query_prefix + text])[0]

    # --- internals ---------------------------------------------------------------------

    def _embed(self, inputs: list[str]) -> list[list[float]]:
        keys = [EmbeddingCache.key(self.spec.slug, t) for t in inputs]
        found = self.cache.get_many(keys) if self.cache else {}
        todo = [i for i, k in enumerate(keys) if k not in found]
        i = 0
        while i < len(todo):
            batch = todo[i : i + self.batch_size]
            try:
                vectors = self._request([inputs[j] for j in batch])
            except _BatchTooLarge:
                if self.batch_size == 1:
                    raise EmbeddingError("the API rejects even single inputs") from None
                self.batch_size = max(1, self.batch_size // 2)
                continue
            new = {keys[j]: v for j, v in zip(batch, vectors, strict=True)}
            if self.cache:
                self.cache.put_many(new)
            found.update(new)
            i += len(batch)
        return [found[k] for k in keys]

    def _request(self, batch: list[str]) -> list[list[float]]:
        for attempt in range(self.max_retries + 1):
            self.requests_made += 1
            try:
                resp = self._client.post(
                    "/embeddings", json={"model": self.spec.slug, "input": batch}
                )
            except httpx.TransportError as exc:
                if attempt == self.max_retries:
                    raise EmbeddingError(f"network error: {type(exc).__name__}") from None
                self._sleep(2**attempt)
                continue
            body = _json_or_empty(resp)
            message = str((body.get("error") or {}).get("message", ""))
            if resp.status_code == 429 or (resp.status_code == 200 and "error" in body):
                if "per-day" in message or "per day" in message:
                    raise QuotaExhausted(
                        f"OpenRouter daily free-model quota exhausted ({message[:120]})"
                    )
                if attempt == self.max_retries:
                    raise EmbeddingError(f"rate limited after {attempt + 1} attempts")
                self._sleep(_retry_after(resp) or 2**attempt)
                continue
            if (
                resp.status_code in (400, 413)
                and len(batch) > 1
                and _looks_like_size_error(message)
            ):
                raise _BatchTooLarge
            if resp.status_code >= 500:
                if attempt == self.max_retries:
                    raise EmbeddingError(f"server error {resp.status_code}")
                self._sleep(2**attempt)
                continue
            if resp.status_code != 200 or "data" not in body:
                raise EmbeddingError(f"HTTP {resp.status_code}: {message[:200] or 'no data'}")
            return self._parse(body, len(batch))
        raise EmbeddingError("unreachable")

    def _parse(self, body: dict, n: int) -> list[list[float]]:
        data = sorted(body["data"], key=lambda d: d["index"])
        if len(data) != n:
            raise EmbeddingError(f"asked for {n} embeddings, got {len(data)}")
        vectors = [d["embedding"] for d in data]
        if any(len(v) != self.spec.dim for v in vectors):
            got = {len(v) for v in vectors}
            raise EmbeddingError(f"{self.spec.slug}: expected dim {self.spec.dim}, got {got}")
        self.prompt_tokens += int((body.get("usage") or {}).get("prompt_tokens", 0))
        return [_l2(v) for v in vectors] if self.spec.normalize else vectors

    def close(self) -> None:
        self._client.close()


class _BatchTooLarge(Exception):
    pass


def _json_or_empty(resp: httpx.Response) -> dict:
    try:
        body = resp.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("retry-after")
    try:
        return min(float(value), 60.0) if value else None
    except ValueError:
        return None


def _looks_like_size_error(message: str) -> bool:
    m = message.lower()
    return any(w in m for w in ("too many", "too large", "batch", "maximum", "exceed", "limit"))
