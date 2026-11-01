"""Check every embedding collection in Qdrant Cloud, and keep the free cluster alive.

Usage: uv run python -m ingestion.verify_index [--model SLUG ...] [--query "..."]

For each model: the collection exists, holds exactly one point per chunk, has
the model's dimension, and a sanity query returns the expected page. Running
it also counts as activity for the free Qdrant cluster, which is suspended
after a week without use. Query embeddings are cached, so a re-run makes no
embedding requests.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import truststore

truststore.inject_into_ssl()

from app import vectorstore  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.embeddings import REGISTRY, EmbeddingCache, EmbeddingError, OpenRouterEmbeddings  # noqa: E402
from ingestion.chunking import ChunkConfig, build_chunks  # noqa: E402

SANITY_QUERY = "How do I close an app?"
SANITY_PAGE = 22


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--model", action="append", help="registry slug (default: all)")
    ap.add_argument("--query", default=SANITY_QUERY)
    ap.add_argument("--expect-page", type=int, default=SANITY_PAGE)
    args = ap.parse_args()

    settings = get_settings()
    cfg_hash, chunks = build_chunks(ChunkConfig())
    client = vectorstore.make_client(
        settings.qdrant_url, settings.qdrant_api_key.get_secret_value()
    )
    cache = EmbeddingCache(Path(settings.cache_dir) / "embeddings.sqlite")
    ok = True
    for slug in args.model or sorted(REGISTRY):
        spec = REGISTRY[slug]
        name = vectorstore.collection_name(slug, cfg_hash)
        st = vectorstore.status(client, name)
        problems = st.problems(expected_dim=spec.dim, expected_points=len(chunks))
        if problems:
            ok = False
            print(f"[FAIL] {slug}: " + "; ".join(problems))
            continue
        embedder = OpenRouterEmbeddings(
            spec, settings.openrouter_api_key.get_secret_value(), cache=cache
        )
        try:
            hits = vectorstore.search(client, name, embedder.embed_query(args.query), k=5)
        except EmbeddingError as exc:
            ok = False
            print(f"[FAIL] {slug}: {exc}")
            continue
        finally:
            embedder.close()
        pages = [h.payload["page"] for h in hits]
        rank = next(
            (i + 1 for i, h in enumerate(hits) if args.expect_page in h.payload["pages"]), None
        )
        verdict = "OK  " if rank is not None and rank <= 3 else "WARN"
        ok &= verdict == "OK  "
        top = hits[0].payload
        print(
            f"[{verdict}] {slug}: {st.points} points, dim {st.dim}; '{args.query}' -> pages "
            f"{pages} (p{args.expect_page} at rank {rank}); top: p{top['page']} "
            f"{' > '.join(top['section_path'][1:]) or top['section_path']} ({hits[0].score:.3f})"
        )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
