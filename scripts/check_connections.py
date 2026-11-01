"""Verify that OpenRouter, Qdrant Cloud and Langfuse are reachable with the keys in .env.

Usage (from the repo root):  uv run python -m scripts.check_connections

Prints one OK / FAIL / SKIP line per service and never prints key values:
exception messages are redacted before printing. Exit code is 0 when every
required service (OpenRouter, Qdrant) is OK; Langfuse is optional.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass

import truststore

truststore.inject_into_ssl()  # use the OS trust store (works behind TLS-inspecting proxies)

import httpx  # noqa: E402

from app.config import OPENROUTER_BASE_URL, ConfigError, Settings, get_settings  # noqa: E402

TIMEOUT_S = 15.0


@dataclass
class Result:
    service: str
    status: str  # OK | FAIL | SKIP
    detail: str


def redact(text: str, settings: Settings) -> str:
    """Remove every secret value from a message before it is printed."""
    for secret in (
        settings.openrouter_api_key,
        settings.qdrant_api_key,
        settings.langfuse_public_key,
        settings.langfuse_secret_key,
    ):
        value = secret.get_secret_value() if secret else ""
        if value:
            text = text.replace(value, "***")
    return text


def check_openrouter(s: Settings) -> Result:
    headers = {"Authorization": f"Bearer {s.openrouter_api_key.get_secret_value()}"}
    with httpx.Client(base_url=OPENROUTER_BASE_URL, headers=headers, timeout=TIMEOUT_S) as client:
        # GET /key validates the key without spending credit.
        resp = client.get("/key")
        if resp.status_code != 200:
            return Result("OpenRouter", "FAIL", f"/key returned HTTP {resp.status_code}")
        info = resp.json()["data"]
        remaining = info.get("limit_remaining")
        credit = "no limit set" if remaining is None else f"limit remaining ${remaining:.2f}"

        chat_ids = {m["id"] for m in client.get("/models").json()["data"]}
        emb_ids = {m["id"] for m in client.get("/embeddings/models").json()["data"]}
    problems = []
    if s.chat_model not in chat_ids:
        problems.append(f"CHAT_MODEL '{s.chat_model}' not found")
    if s.embedding_model not in emb_ids:
        problems.append(f"EMBEDDING_MODEL '{s.embedding_model}' not found")
    if problems:
        return Result("OpenRouter", "FAIL", "key valid, but " + "; ".join(problems))
    return Result(
        "OpenRouter",
        "OK",
        f"key valid ({credit}, used ${info.get('usage', 0):.4f}); "
        f"chat={s.chat_model}, embedding={s.embedding_model} available",
    )


def check_qdrant(s: Settings) -> Result:
    from qdrant_client import QdrantClient

    client = QdrantClient(
        url=s.qdrant_url, api_key=s.qdrant_api_key.get_secret_value(), timeout=int(TIMEOUT_S)
    )
    try:
        names = sorted(c.name for c in client.get_collections().collections)
    finally:
        client.close()
    listed = ", ".join(names) if names else "none yet"
    return Result("Qdrant", "OK", f"cluster reachable; {len(names)} collection(s): {listed}")


def check_langfuse(s: Settings) -> Result:
    if not s.langfuse_enabled:
        return Result("Langfuse", "SKIP", "keys not set; tracing will be disabled (optional)")
    from langfuse import Langfuse

    client = Langfuse(
        public_key=s.langfuse_public_key.get_secret_value(),
        secret_key=s.langfuse_secret_key.get_secret_value(),
        base_url=s.langfuse_base_url,
        timeout=int(TIMEOUT_S),
    )
    try:
        ok = client.auth_check()
    finally:
        client.shutdown()
    if not ok:
        return Result("Langfuse", "FAIL", f"auth_check failed against {s.langfuse_base_url}")
    return Result("Langfuse", "OK", f"credentials valid for {s.langfuse_base_url}")


def run_check(name: str, fn: Callable[[Settings], Result], s: Settings) -> Result:
    try:
        return fn(s)
    except Exception as exc:  # report every failure as a line, never a stack trace
        return Result(name, "FAIL", redact(f"{type(exc).__name__}: {exc}", s))


def main() -> int:
    try:
        settings = get_settings()
    except ConfigError as exc:
        print(exc)
        return 1

    results = [
        run_check("OpenRouter", check_openrouter, settings),
        run_check("Qdrant", check_qdrant, settings),
        run_check("Langfuse", check_langfuse, settings),
    ]
    for r in results:
        print(f"[{r.status:<4}] {r.service:<10} {r.detail}")

    required_ok = all(r.status == "OK" for r in results if r.service != "Langfuse")
    langfuse_ok = results[2].status != "FAIL"
    return 0 if required_ok and langfuse_ok else 1


if __name__ == "__main__":
    sys.exit(main())
