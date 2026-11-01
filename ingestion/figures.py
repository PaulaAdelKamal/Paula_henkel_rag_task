"""Figure regions and their text anchors (no OCR, no vision model; see ADR-0013).

Usage: uv run python -m ingestion.figures [--pdf PATH] [--pages data/parsed/pages.jsonl]

Writes data/parsed/figures.jsonl, one record per figure region:
  figure_id, page, bbox, kind, decision, anchor_element, anchor_text,
  section_path, labels, duplicate_of

Why regions and not image objects: many drawings are assembled from many image
pieces (the two iPhone drawings on p. 8 are 88 thin slices), so neighbouring
placements are merged into one region. The guide prints no captions; the de
facto caption is the instruction sentence right above the figure, recorded as
`anchor_text`. Callout labels inside the region were already extracted as
`figure_labels` elements by parse_pdf and are attached here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

import pymupdf

DEFAULT_PDF = Path("data/source/iPhone_User_Guide_1.pdf")
DEFAULT_PAGES = Path("data/parsed/pages.jsonl")
DEFAULT_OUT = Path("data/parsed/figures.jsonl")

MERGE_GAP = 8.0  # pt: image placements closer than this belong to one figure
MIN_SIDE = 0.5  # pt: zero-size placements are artefacts
ANCHOR_TYPES = {"paragraph", "list_item", "table_row", "section", "subsection", "chapter_title"}


@dataclass
class Figure:
    figure_id: str  # "p022-f1"
    page: int
    bbox: list[float]
    kind: str  # figure | small_graphic | app_icon | icon
    decision: str  # keep | drop (with the reason in `kind`)
    anchor_element: int | None  # index into the page's elements
    anchor_text: str | None
    section_path: list[str]
    labels: list[str]
    duplicate_of: str | None  # earlier figure with an identical rendering


def merge_boxes(boxes: list[list[float]], gap: float = MERGE_GAP) -> list[list[float]]:
    """Union boxes that overlap or are closer than `gap`, until nothing changes."""
    merged = [list(b) for b in boxes]
    changed = True
    while changed:
        changed = False
        for i in range(len(merged)):
            for j in range(i + 1, len(merged)):
                a, b = merged[i], merged[j]
                if (
                    a[0] - gap <= b[2]
                    and b[0] - gap <= a[2]
                    and a[1] - gap <= b[3]
                    and b[1] - gap <= a[3]
                ):
                    merged[i] = [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]
                    del merged[j]
                    changed = True
                    break
            if changed:
                break
    return sorted(merged, key=lambda b: (b[1], b[0]))


def classify(w: float, h: float, is_opener: bool) -> tuple[str, str]:
    if w < 20 and h < 20:
        return "icon", "drop"
    if is_opener and w < 60 and h < 60:
        return "app_icon", "drop"  # the chapter title already names the app
    if w < 60 and h < 60:
        return "small_graphic", "keep"
    return "figure", "keep"


def find_anchor(elements: list[dict], bbox: list[float]) -> int | None:
    """The text element right above the figure (the instruction it illustrates).

    Falls back to the first element below when the figure starts the page.
    """
    above = [
        (e["bbox"][3], i)
        for i, e in enumerate(elements)
        if e["type"] in ANCHOR_TYPES and e["bbox"][3] <= bbox[1] + 2
    ]
    if above:
        return max(above)[1]
    below = [
        (e["bbox"][1], i)
        for i, e in enumerate(elements)
        if e["type"] in ANCHOR_TYPES and e["bbox"][1] >= bbox[3] - 2
    ]
    return min(below)[1] if below else None


def _group_by_anchor(
    regions: list[list[float]], elements: list[dict], is_opener: bool
) -> list[tuple[list[float], str, str, int | None]]:
    """One figure per illustrated text element.

    Views of one drawing can sit far apart (p. 8 draws each iPhone as top edge,
    front, back and bottom edge), but they all illustrate the same instruction,
    so kept regions sharing an anchor element are unioned into one figure.
    """
    out: list[tuple[list[float], str, str, int | None]] = []
    by_anchor: dict[int, int] = {}  # anchor element -> index in `out`
    for bbox in regions:
        kind, decision = classify(bbox[2] - bbox[0], bbox[3] - bbox[1], is_opener)
        anchor = find_anchor(elements, bbox) if decision == "keep" else None
        if anchor is not None and anchor in by_anchor:
            prev_bbox, prev_kind, prev_decision, _ = out[by_anchor[anchor]]
            union = [
                min(prev_bbox[0], bbox[0]),
                min(prev_bbox[1], bbox[1]),
                max(prev_bbox[2], bbox[2]),
                max(prev_bbox[3], bbox[3]),
            ]
            kind = "figure" if "figure" in (prev_kind, kind) else prev_kind
            out[by_anchor[anchor]] = (union, kind, prev_decision, anchor)
            continue
        if anchor is not None:
            by_anchor[anchor] = len(out)
        out.append((bbox, kind, decision, anchor))
    return out


def _follows(elements: list[dict], anchor: int | None, i: int) -> bool:
    """Labels element directly after the anchor (parse_pdf emits a figure's labels there)."""
    return (
        anchor is not None
        and i > anchor
        and all(elements[k]["type"] == "figure_labels" for k in range(anchor + 1, i + 1))
    )


def _overlaps(a: list[float], b: list[float], pad: float = 4.0) -> bool:
    return a[0] - pad <= b[2] and b[0] - pad <= a[2] and a[1] - pad <= b[3] and b[1] - pad <= a[3]


def extract_figures(pdf_path: Path, pages: list[dict]) -> list[Figure]:
    openers = {p["page"] for p in pages if p["content_type"] == "chapter_opener"}
    retrievable = {"content", "chapter_opener", "legal"}
    figures: list[Figure] = []
    seen: dict[str, str] = {}  # rendering digest -> first figure_id (dedupe by content)
    with pymupdf.open(pdf_path) as doc:
        for page in pages:
            if page["content_type"] not in retrievable:
                continue
            page_obj = doc[page["pdf_page_index"]]
            boxes = [
                list(info["bbox"])
                for info in page_obj.get_image_info()
                if info["bbox"][2] - info["bbox"][0] > MIN_SIDE
                and info["bbox"][3] - info["bbox"][1] > MIN_SIDE
            ]
            elements = page["elements"]
            for n, (bbox, kind, decision, anchor) in enumerate(
                _group_by_anchor(merge_boxes(boxes), elements, page["page"] in openers), start=1
            ):
                labels = [
                    e["text"].removeprefix("Figure labels: ")
                    for i, e in enumerate(elements)
                    if e["type"] == "figure_labels"
                    and (_overlaps(e["bbox"], bbox, pad=25) or _follows(elements, anchor, i))
                ]
                pix = page_obj.get_pixmap(clip=pymupdf.Rect(bbox), dpi=72)
                digest = hashlib.sha256(pix.samples).hexdigest()
                figure_id = f"p{page['page']:03d}-f{n}"
                duplicate_of = seen.setdefault(digest, figure_id)
                figures.append(
                    Figure(
                        figure_id=figure_id,
                        page=page["page"],
                        bbox=[round(v, 1) for v in bbox],
                        kind=kind,
                        decision=decision,
                        anchor_element=anchor,
                        anchor_text=elements[anchor]["text"] if anchor is not None else None,
                        section_path=(
                            elements[anchor]["section_path"]
                            if anchor is not None
                            else page["section_path_start"]
                        ),
                        labels=labels,
                        duplicate_of=duplicate_of if duplicate_of != figure_id else None,
                    )
                )
    return figures


def load_pages(path: Path = DEFAULT_PAGES) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pdf", type=Path, default=DEFAULT_PDF)
    ap.add_argument("--pages", type=Path, default=DEFAULT_PAGES)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()
    figures = extract_figures(args.pdf, load_pages(args.pages))
    with args.out.open("w", encoding="utf-8", newline="\n") as f:
        for fig in figures:
            f.write(json.dumps(asdict(fig), ensure_ascii=False) + "\n")
    kept = [f for f in figures if f.decision == "keep"]
    kinds: dict[str, int] = {}
    for fig in figures:
        kinds[fig.kind] = kinds.get(fig.kind, 0) + 1
    print(f"regions={len(figures)} kept={len(kept)} on {len({f.page for f in kept})} pages {kinds}")
    print(f"kept with anchor text: {sum(1 for f in kept if f.anchor_text)}")
    print(f"kept with callout labels: {sum(1 for f in kept if f.labels)}")
    print(f"duplicates (identical rendering): {sum(1 for f in figures if f.duplicate_of)}")


if __name__ == "__main__":
    main()
