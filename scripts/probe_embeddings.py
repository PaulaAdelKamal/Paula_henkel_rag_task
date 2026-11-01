"""Probe each free embedding model once before indexing (1 request per model).

Usage: uv run python -m scripts.probe_embeddings [--model SLUG ...]

One batched request per model with three inputs answers what the model cards
can't tell us about OpenRouter's hosted endpoints:
  [0] the raw query                    -> dimension and whether vectors are normalised
  [1] the same query with "query: "    -> if cos([0],[1]) ~ 1.0 the prefix is added (or
                                          ignored) upstream and must not be added again
  [2] the longest chunk, doc-prefixed  -> real token count vs the BERT estimate
Writes data/reports/embedding_probe.json.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict
from pathlib import Path

import truststore

truststore.inject_into_ssl()

import httpx  # noqa: E402

from app.config import OPENROUTER_BASE_URL, get_settings  # noqa: E402
from app.embeddings import REGISTRY  # noqa: E402
from ingestion.chunking import ChunkConfig, build_chunks, count_tokens  # noqa: E402

QUERY = "How do I close an app?"
OUT = Path("data/reports/embedding_probe.json")


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--model", action="append")
    args = ap.parse_args()
    key = get_settings().openrouter_api_key.get_secret_value()
    _, chunks = build_chunks(ChunkConfig())
    longest = max(chunks, key=lambda c: c.embed_token_count)
    results = []
    with httpx.Client(
        base_url=OPENROUTER_BASE_URL, headers={"Authorization": f"Bearer {key}"}, timeout=60
    ) as client:
        for slug in args.model or sorted(REGISTRY):
            spec = REGISTRY[slug]
            inputs = [QUERY, spec.query_prefix + QUERY, spec.doc_prefix + longest.embed_text]
            resp = client.post("/embeddings", json={"model": slug, "input": inputs})
            body = (
                resp.json()
                if resp.headers.get("content-type", "").startswith("application/json")
                else {}
            )
            if resp.status_code != 200 or "data" not in body:
                msg = (body.get("error") or {}).get("message", resp.text[:200])
                results.append(
                    {"model": slug, "ok": False, "status": resp.status_code, "error": msg}
                )
                print(f"[FAIL] {slug}: HTTP {resp.status_code} {msg[:160]}")
                continue
            vecs = [d["embedding"] for d in sorted(body["data"], key=lambda d: d["index"])]
            usage = (body.get("usage") or {}).get("prompt_tokens")
            short_bert = count_tokens(inputs[0]) + count_tokens(inputs[1])
            r = {
                "model": slug,
                "ok": True,
                "dim": len(vecs[0]),
                "dim_matches_registry": len(vecs[0]) == spec.dim,
                "norm_raw_query": round(math.sqrt(sum(x * x for x in vecs[0])), 4),
                "cos_raw_vs_prefixed_query": round(cosine(vecs[0], vecs[1]), 4),
                "api_prompt_tokens_total": usage,
                "bert_tokens_total": short_bert + count_tokens(inputs[2]),
                "longest_chunk_bert_tokens": longest.embed_token_count,
                "spec": asdict(spec),
            }
            results.append(r)
            print(
                f"[OK]   {slug}: dim={r['dim']} (registry {spec.dim}), norm={r['norm_raw_query']}, "
                f"cos(raw, prefixed)={r['cos_raw_vs_prefixed_query']}, "
                f"tokens api={usage} vs bert={r['bert_tokens_total']}"
            )
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
