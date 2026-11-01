"""Upload the golden set to a Langfuse dataset (idempotent).

Usage: uv run python -m eval.datasets.upload_langfuse [--dataset golden] [--dry-run]

Each item is upserted with a deterministic id ("golden-g001"), so re-running
after a review updates items in place; items rejected in review are set to
ARCHIVED instead of being deleted. Step 9's experiments run against this
dataset, so every Langfuse experiment run is comparable item by item.
Uses only the Langfuse keys (no OpenRouter quota).
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter

import truststore

truststore.inject_into_ssl()

from app.config import get_settings  # noqa: E402
from eval.datasets.build import DRAFT, build  # noqa: E402
from eval.datasets.review import load  # noqa: E402
from eval.datasets.schema import GoldenItem  # noqa: E402

DESCRIPTION = (
    "Golden evaluation set for the iOS 7.1 iPhone User Guide RAG chatbot. Questions drafted by "
    "Claude from the parsed guide, every evidence quote verified on its page by "
    "eval/datasets/build.py, reviewed by a person (see metadata.review_status)."
)


def item_payload(it: GoldenItem) -> dict:
    return {
        "id": f"golden-{it.id}",
        "input": {
            "question": it.question,
            "history": [t.model_dump() for t in it.history],
        },
        "expected_output": {
            "reference_answer": it.reference_answer,
            "answerable": it.answerable,
            "gold_pages": it.gold_pages,
            "gold_sections": it.gold_sections,
        },
        "metadata": {
            "category": it.category,
            "difficulty": it.difficulty,
            "review_status": it.review_status,
            "evidence": [e.model_dump() for e in it.evidence],
            "notes": it.notes,
            "source": it.source,
        },
        "status": "ACTIVE",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dataset", default="golden")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    items, problems = build()
    if problems:
        print("golden draft has errors; run `python -m eval.datasets.build` first")
        return 1
    rejected = [it.id for it in load(DRAFT) if it.review_status == "rejected"]
    payloads = [item_payload(it) for it in items]
    print(f"{len(payloads)} active items, {len(rejected)} to archive -> dataset '{args.dataset}'")
    print("review status:", dict(Counter(it.review_status for it in items)))
    if args.dry_run:
        return 0

    s = get_settings()
    if not s.langfuse_enabled:
        print("LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY are not set")
        return 1
    from langfuse import Langfuse

    lf = Langfuse(
        public_key=s.langfuse_public_key.get_secret_value(),
        secret_key=s.langfuse_secret_key.get_secret_value(),
        base_url=s.langfuse_base_url,
        tracing_enabled=False,
    )
    try:
        lf.create_dataset(
            name=args.dataset,
            description=DESCRIPTION,
            metadata={
                "items": len(payloads),
                "unanswerable": sum(not it.answerable for it in items),
            },
        )
        for p in payloads:
            lf.create_dataset_item(dataset_name=args.dataset, **p)
        for item_id in rejected:
            lf.create_dataset_item(
                dataset_name=args.dataset, id=f"golden-{item_id}", status="ARCHIVED"
            )
        ds = lf.get_dataset(args.dataset)
        active = [i for i in ds.items if str(getattr(i, "status", "ACTIVE")).endswith("ACTIVE")]
        print(f"Langfuse dataset '{args.dataset}': {len(ds.items)} items ({len(active)} active)")
    finally:
        lf.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
