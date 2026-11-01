"""Review the golden draft: an HTML page, a CSV sheet, or an interactive CLI.

Usage:
  uv run python -m eval.datasets.review html          # data/cache/review/review.html
  uv run python -m eval.datasets.review export        # eval/datasets/review_sheet.csv
  uv run python -m eval.datasets.review import        # apply the edited CSV to golden_draft.jsonl
  uv run python -m eval.datasets.review cli           # accept / edit / reject item by item

Every path ends with `python -m eval.datasets.build`, which re-validates the
evidence and rewrites golden.jsonl (rejected items are left out). A changed
question or answer turns an accepted item into "edited", so the dataset
records what the reviewer changed.
"""

from __future__ import annotations

import argparse
import base64
import csv
import html
import sys
from pathlib import Path

from eval.datasets.schema import DraftItem

HERE = Path(__file__).parent
DRAFT = HERE / "golden_draft.jsonl"
SHEET = HERE / "review_sheet.csv"
REVIEW_DIR = Path("data/cache/review")
CSV_FIELDS = [
    "id",
    "category",
    "difficulty",
    "answerable",
    "history",
    "question",
    "reference_answer",
    "evidence",
    "review_status",
    "reviewer_notes",
]


def load(path: Path = DRAFT) -> list[DraftItem]:
    with path.open(encoding="utf-8") as f:
        return [DraftItem.model_validate_json(line) for line in f if line.strip()]


def save(items: list[DraftItem], path: Path = DRAFT) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for it in items:
            f.write(it.model_dump_json() + "\n")


def apply_decision(
    item: DraftItem,
    status: str,
    question: str | None = None,
    answer: str | None = None,
    notes: str | None = None,
) -> DraftItem:
    """Return the item with the reviewer's decision; edits to text mark it 'edited'."""
    changes: dict = {"reviewer_notes": notes if notes is not None else item.reviewer_notes}
    edited = False
    if question is not None and question.strip() and question.strip() != item.question:
        changes["question"], edited = question.strip(), True
    if answer is not None and answer.strip() and answer.strip() != item.reference_answer:
        changes["reference_answer"], edited = answer.strip(), True
    if status not in {"draft", "accepted", "edited", "rejected"}:
        raise ValueError(f"unknown review status {status!r}")
    changes["review_status"] = "edited" if edited and status in {"accepted", "edited"} else status
    return item.model_copy(update=changes)


# --------------------------------------------------------------------------- CSV


def export_csv(items: list[DraftItem], path: Path = SHEET) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:  # BOM so Excel reads UTF-8
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for it in items:
            w.writerow(
                {
                    "id": it.id,
                    "category": it.category,
                    "difficulty": it.difficulty,
                    "answerable": it.answerable,
                    "history": " / ".join(f"{t.role}: {t.content}" for t in it.history),
                    "question": it.question,
                    "reference_answer": it.reference_answer,
                    "evidence": " | ".join(f"p{e.page}: {e.quote}" for e in it.evidence),
                    "review_status": it.review_status,
                    "reviewer_notes": it.reviewer_notes,
                }
            )


def import_csv(items: list[DraftItem], path: Path = SHEET) -> list[DraftItem]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = {r["id"]: r for r in csv.DictReader(f)}
    out = []
    for it in items:
        r = rows.get(it.id)
        if r is None:
            out.append(it)
            continue
        out.append(
            apply_decision(
                it,
                r["review_status"].strip() or it.review_status,
                question=r["question"],
                answer=r["reference_answer"],
                notes=r["reviewer_notes"],
            )
        )
    return out


# --------------------------------------------------------------------------- HTML


def _page_data_uri(page: int) -> str:
    from app.pages import page_png

    return "data:image/png;base64," + base64.b64encode(page_png(page, scale=1.0)).decode()


def export_html(items: list[DraftItem], path: Path = REVIEW_DIR / "review.html") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    cards = []
    for it in items:
        hist = "".join(
            f"<div class='turn'><b>{t.role}:</b> {html.escape(t.content)}</div>" for t in it.history
        )
        evid = (
            "".join(f"<li><b>p{e.page}</b>: “{html.escape(e.quote)}”</li>" for e in it.evidence)
            or "<li><i>none: must be refused</i></li>"
        )
        pages = sorted({e.page for e in it.evidence})
        imgs = "".join(
            f"<figure><img loading='lazy' src='{_page_data_uri(p)}' alt='page {p}'>"
            f"<figcaption>Page {p}</figcaption></figure>"
            for p in pages
        )
        cards.append(
            f"<section class='card {it.review_status}' id='{it.id}'>"
            f"<h2>{it.id} · {it.category} · {it.difficulty}"
            f"{'' if it.answerable else ' · <span class=neg>unanswerable</span>'}"
            f" · <span class=status>{it.review_status}</span></h2>"
            f"{hist}<p class='q'>{html.escape(it.question)}</p>"
            f"<p><b>Reference answer:</b> {html.escape(it.reference_answer)}</p>"
            f"<p><b>Evidence:</b></p><ul>{evid}</ul>"
            f"{'<p class=notes>' + html.escape(it.notes) + '</p>' if it.notes else ''}"
            f"<div class='pages'>{imgs}</div></section>"
        )
    path.write_text(
        "<!doctype html><meta charset='utf-8'><title>Golden set review</title>"
        "<style>body{font:15px/1.5 system-ui,sans-serif;max-width:1100px;margin:auto;"
        "padding:16px;background:#fcfcfb;color:#0b0b0b}.card{border:1px solid #ddd;"
        "border-radius:8px;padding:12px 16px;margin:16px 0;background:#fff}"
        "h2{font-size:15px;color:#52514e;margin:0 0 8px}.q{font-size:18px;font-weight:600}"
        ".turn{color:#52514e}.neg{color:#b42318}.notes{color:#52514e;font-style:italic}"
        ".pages{display:flex;gap:12px;flex-wrap:wrap}figure{margin:0}"
        "img{width:420px;max-width:100%;border:1px solid #ccc}</style>"
        f"<h1>Golden set review ({len(items)} items)</h1>"
        "<p>Check each question against its page: is the evidence on that page, is it the best "
        "page, and is the reference answer right? Record decisions with "
        "<code>review export</code>/<code>import</code> or <code>review cli</code>.</p>"
        + "".join(cards),
        encoding="utf-8",
    )
    return path


# --------------------------------------------------------------------------- CLI


def interactive(items: list[DraftItem], path: Path = DRAFT) -> None:
    todo = [i for i, it in enumerate(items) if it.review_status == "draft"]
    print(f"{len(todo)} items to review (a=accept, e=edit, r=reject, s=skip, q=quit)")
    for i in todo:
        it = items[i]
        print("\n" + "=" * 80)
        print(f"{it.id} · {it.category} · {it.difficulty} · answerable={it.answerable}")
        for t in it.history:
            print(f"  [{t.role}] {t.content}")
        print(f"Q: {it.question}\nA: {it.reference_answer}")
        for e in it.evidence:
            print(f"  evidence p{e.page}: {e.quote}")
        choice = input("decision [a/e/r/s/q]: ").strip().lower()
        if choice == "q":
            break
        if choice == "s" or choice not in {"a", "e", "r"}:
            continue
        notes = input("notes (optional): ").strip() or None
        if choice == "e":
            q = input("new question (blank = keep): ")
            a = input("new reference answer (blank = keep): ")
            items[i] = apply_decision(it, "edited", question=q, answer=a, notes=notes)
        else:
            items[i] = apply_decision(it, "accepted" if choice == "a" else "rejected", notes=notes)
        save(items, path)  # progress is never lost
    print("saved; now run: uv run python -m eval.datasets.build")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", choices=["html", "export", "import", "cli"])
    args = ap.parse_args()
    items = load()
    if args.command == "html":
        print(f"wrote {export_html(items)}")
    elif args.command == "export":
        export_csv(items)
        print(f"wrote {SHEET}; edit review_status (accepted/edited/rejected) and text, then import")
    elif args.command == "import":
        updated = import_csv(items)
        save(updated)
        changed = sum(1 for a, b in zip(items, updated, strict=True) if a != b)
        print(
            f"applied {changed} change(s) to {DRAFT}; now run: uv run python -m eval.datasets.build"
        )
    else:
        interactive(items)
    return 0


if __name__ == "__main__":
    sys.exit(main())
