"""Embed chunks and sync them into one Qdrant collection per embedding model.

Usage:
  uv run python -m ingestion.ingest --all --plan     # no embedding calls: prints the request budget
  uv run python -m ingestion.ingest --all --yes      # embed + upsert (idempotent)
  uv run python -m ingestion.ingest --model nvidia/nemotron-3.5-embed-1b:free --yes

Idempotent: point ids are the chunks' deterministic UUIDv5s, so a re-run only
embeds and upserts chunks that are missing and deletes stale ones; vectors are
cached on disk by content hash (data/cache/embeddings.sqlite), so nothing is
requested twice. Writes data/reports/ingestion_<model>.json per model.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from dataclasses import asdict
from pathlib import Path

import truststore

truststore.inject_into_ssl()

import httpx  # noqa: E402

from app import vectorstore  # noqa: E402
from app.config import OPENROUTER_BASE_URL, get_settings  # noqa: E402
from app.embeddings import (  # noqa: E402
    REGISTRY,
    EmbeddingCache,
    EmbeddingError,
    OpenRouterEmbeddings,
    get_spec,
)
from ingestion.chunking import ChunkConfig, build_chunks  # noqa: E402

REPORTS = Path("data/reports")
BATCH_SIZE = 64


def free_quota(api_key: str) -> dict:
    resp = httpx.get(
        f"{OPENROUTER_BASE_URL}/key", headers={"Authorization": f"Bearer {api_key}"}, timeout=20
    )
    resp.raise_for_status()
    data = resp.json()["data"]
    return {
        "is_free_tier": data.get("is_free_tier"),
        **(data.get("free_model_daily_requests") or {}),
    }


def plan_model(
    slug: str, chunks: list[dict], cfg_hash: str, client, cache: EmbeddingCache, batch_size: int
) -> dict:
    spec = get_spec(slug)
    name = vectorstore.collection_name(slug, cfg_hash)
    present = vectorstore.existing_ids(client, name) if client.collection_exists(name) else set()
    to_add, to_delete = vectorstore.plan_sync([c["chunk_id"] for c in chunks], present)
    add_set = set(to_add)
    texts = [spec.doc_prefix + c["embed_text"] for c in chunks if c["chunk_id"] in add_set]
    cached = cache.get_many([EmbeddingCache.key(slug, t) for t in texts])
    uncached = len(texts) - len(cached)
    too_long = [c["chunk_id"] for c in chunks if c["embed_token_count"] > spec.max_tokens - 16]
    return {
        "model": slug,
        "collection": name,
        "points_now": len(present),
        "to_add": len(to_add),
        "to_delete": len(to_delete),
        "cached": len(cached),
        "to_embed": uncached,
        "requests": math.ceil(uncached / batch_size),
        "chunks_over_model_limit": len(too_long),
    }


def ingest_model(
    slug: str,
    chunks: list[dict],
    cfg_hash: str,
    client,
    cache: EmbeddingCache,
    api_key: str,
    batch_size: int = BATCH_SIZE,
    max_retries: int = 5,
) -> dict:
    spec = get_spec(slug)
    name = vectorstore.collection_name(slug, cfg_hash)
    started = time.perf_counter()
    created = vectorstore.ensure_collection(client, name, spec.dim)
    present = vectorstore.existing_ids(client, name)
    to_add, to_delete = vectorstore.plan_sync([c["chunk_id"] for c in chunks], present)
    if not to_add and not to_delete:
        # Nothing to do: keep the report of the run that actually built the collection.
        return {
            "model": slug,
            "collection": name,
            "up_to_date": True,
            "points_after": len(present),
            "added": 0,
            "deleted": 0,
            "api_requests": 0,
            "api_prompt_tokens": 0,
            "bert_tokens_embedded": 0,
            "duration_s": round(time.perf_counter() - started, 1),
        }
    add_set = set(to_add)
    new_chunks = [c for c in chunks if c["chunk_id"] in add_set]
    embedder = OpenRouterEmbeddings(
        spec, api_key, batch_size=batch_size, max_retries=max_retries, cache=cache
    )
    try:
        vectors = embedder.embed_documents([c["embed_text"] for c in new_chunks])
    finally:
        embedder.close()
    vectorstore.upsert(client, name, new_chunks, vectors, slug)
    vectorstore.delete(client, name, to_delete)
    st = vectorstore.status(client, name)
    report = {
        "model": slug,
        "collection": name,
        "chunk_config_hash": cfg_hash,
        "dim": spec.dim,
        "created_collection": created,
        "chunks": len(chunks),
        "points_after": st.points,
        "added": len(to_add),
        "deleted": len(to_delete),
        "api_requests": embedder.requests_made,
        "api_prompt_tokens": embedder.prompt_tokens,
        "bert_tokens_embedded": sum(c["embed_token_count"] for c in new_chunks),
        "cost_usd": 0.0,
        "duration_s": round(time.perf_counter() - started, 1),
        "spec": asdict(spec),
    }
    REPORTS.mkdir(parents=True, exist_ok=True)
    out = REPORTS / f"ingestion_{vectorstore.collection_name(slug, cfg_hash).split('__')[1]}.json"
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8", newline="\n")
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    who = ap.add_mutually_exclusive_group(required=True)
    who.add_argument("--model", action="append", help="registry slug (repeatable)")
    who.add_argument("--all", action="store_true", help="every model in the registry")
    ap.add_argument("--plan", action="store_true", help="print the request budget, no API calls")
    ap.add_argument("--yes", action="store_true", help="actually embed and upsert")
    ap.add_argument("--batch-size", type=int, default=BATCH_SIZE, help="inputs per request")
    ap.add_argument("--max-retries", type=int, default=5, help="retries per failed request")
    args = ap.parse_args()

    settings = get_settings()
    api_key = settings.openrouter_api_key.get_secret_value()
    slugs = sorted(REGISTRY) if args.all else args.model
    cfg_hash, chunk_objs = build_chunks(ChunkConfig())
    chunks = [asdict(c) for c in chunk_objs]
    client = vectorstore.make_client(
        settings.qdrant_url, settings.qdrant_api_key.get_secret_value()
    )
    cache = EmbeddingCache(Path(settings.cache_dir) / "embeddings.sqlite")

    plans = [plan_model(s, chunks, cfg_hash, client, cache, args.batch_size) for s in slugs]
    quota = free_quota(api_key)
    total = sum(p["requests"] for p in plans)
    print(f"chunk config {cfg_hash}: {len(chunks)} chunks")
    for p in plans:
        print(
            f"  {p['model']:<44} points={p['points_now']:<4} add={p['to_add']:<4} "
            f"delete={p['to_delete']:<3} cached={p['cached']:<4} embed={p['to_embed']:<4} "
            f"requests~{p['requests']}  over_limit={p['chunks_over_model_limit']}"
        )
    print(f"estimated embedding requests: {total} (cost $0.00, free models)")
    print(f"free-model quota today: {quota.get('remaining')} of {quota.get('limit')} remaining")
    if args.plan or not args.yes:
        print("plan only; re-run with --yes to embed and upsert")
        return 0
    if quota.get("remaining") is not None and total > quota["remaining"]:
        print("not enough free-model quota left today; aborting before any request")
        return 1
    for slug in slugs:
        try:
            r = ingest_model(
                slug, chunks, cfg_hash, client, cache, api_key, args.batch_size, args.max_retries
            )
        except EmbeddingError as exc:
            print(f"[FAIL] {slug}: {exc}")
            return 1
        print(
            f"[OK]   {slug}: {r['points_after']} points (+{r['added']} -{r['deleted']}), "
            f"{r['api_requests']} requests, {r['api_prompt_tokens']} API tokens "
            f"vs {r['bert_tokens_embedded']} BERT tokens, {r['duration_s']}s"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
