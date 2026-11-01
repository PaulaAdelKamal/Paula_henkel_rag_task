"""Validate the golden draft and build golden.jsonl.

Usage: uv run python -m eval.datasets.build [--draft PATH] [--out PATH]

For every item in golden_draft.jsonl:
- schema check (eval/datasets/schema.py) and unique ids;
- answerable items: every evidence quote must appear verbatim (after whitespace,
  quote-mark and case normalisation) on its page in data/parsed/pages.jsonl, and
  that page must be retrievable content (not the cover or the table of contents);
- unanswerable items: no evidence, and the reference answer says so;
- multi-turn items: a history that starts with the user and alternates.
Gold pages are the evidence pages; gold sections are the section paths of the
elements that contain the quotes. Nothing positional is typed by hand.
Writes golden.jsonl (rejected items excluded) and exits 1 on any error.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path

from pydantic import ValidationError

from eval.datasets.schema import DraftItem, GoldenItem

HERE = Path(__file__).parent
DRAFT = HERE / "golden_draft.jsonl"
OUT = HERE / "golden.jsonl"
PAGES = Path("data/parsed/pages.jsonl")
RETRIEVABLE = {"content", "chapter_opener", "legal"}
_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"'})


def norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).translate(_QUOTES)
    return re.sub(r"\s+", " ", text).strip().casefold()


def load_pages(path: Path = PAGES) -> dict[int, dict]:
    with path.open(encoding="utf-8") as f:
        return {p["page"]: p for p in map(json.loads, f)}


def locate(quote: str, page: dict) -> list[str] | None:
    """Section path of the element containing the quote; None if the quote isn't on the page."""
    q = norm(quote)
    for e in page["elements"]:
        if q in norm(e["text"]):
            return e["section_path"]
    if q in norm(" ".join(e["text"] for e in page["elements"])):
        return page["section_path_start"]  # quote spans two elements
    return None


def check(item: DraftItem, pages: dict[int, dict]) -> tuple[list[str], GoldenItem | None]:
    errors: list[str] = []
    sections: list[str] = []
    if item.answerable:
        if not item.evidence:
            errors.append("answerable item without evidence")
        for ev in item.evidence:
            page = pages.get(ev.page)
            if page is None or page["content_type"] not in RETRIEVABLE:
                errors.append(f"p{ev.page} is not retrievable content")
                continue
            path = locate(ev.quote, page)
            if path is None:
                errors.append(f"quote not found on p{ev.page}: {ev.quote[:70]!r}")
            elif path and " > ".join(path) not in sections:
                sections.append(" > ".join(path))
    else:
        if item.evidence:
            errors.append("unanswerable item must not have evidence")
        if "not" not in item.reference_answer.lower():
            errors.append("unanswerable reference answer should say it is not in the guide")
    if item.category == "unanswerable" and item.answerable:
        errors.append("category unanswerable but answerable=true")
    if item.category == "multi_turn":
        roles = [t.role for t in item.history]
        if (
            not roles
            or roles[0] != "user"
            or any(a == b for a, b in zip(roles, roles[1:], strict=False))
        ):
            errors.append("multi_turn history must start with the user and alternate")
    if errors:
        return errors, None
    gold_pages = sorted({ev.page for ev in item.evidence})
    return [], GoldenItem(**item.model_dump(), gold_pages=gold_pages, gold_sections=sections)


def build(draft: Path = DRAFT, pages_path: Path = PAGES) -> tuple[list[GoldenItem], list[str]]:
    pages = load_pages(pages_path)
    items: list[GoldenItem] = []
    problems: list[str] = []
    seen: set[str] = set()
    with draft.open(encoding="utf-8") as f:
        for n, line in enumerate(f, start=1):
            if not line.strip():
                continue
            try:
                item = DraftItem.model_validate_json(line)
            except ValidationError as exc:
                problems.append(f"line {n}: schema: {exc.errors()[0]['msg']}")
                continue
            if item.id in seen:
                problems.append(f"{item.id}: duplicate id")
            seen.add(item.id)
            errors, golden = check(item, pages)
            problems += [f"{item.id}: {e}" for e in errors]
            if golden is not None and item.review_status != "rejected":
                items.append(golden)
    return items, problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--draft", type=Path, default=DRAFT)
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()
    items, problems = build(args.draft)
    for p in problems:
        print(f"[ERROR] {p}")
    if problems:
        return 1
    with args.out.open("w", encoding="utf-8", newline="\n") as f:
        for it in items:
            f.write(it.model_dump_json() + "\n")
    cats = Counter(it.category for it in items)
    status = Counter(it.review_status for it in items)
    unans = sum(1 for it in items if not it.answerable)
    print(f"wrote {args.out}: {len(items)} items, {unans} unanswerable ({unans / len(items):.0%})")
    print("categories:", dict(sorted(cats.items())))
    print("review status:", dict(status))
    return 0


if __name__ == "__main__":
    sys.exit(main())
