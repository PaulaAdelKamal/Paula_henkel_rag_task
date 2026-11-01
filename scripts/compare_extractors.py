"""Compare PDF text extractors on the hard pages of the guide.

Usage: uv run python -m scripts.compare_extractors [path/to/pdf]

Each page has hand-written anchors taken from the rendered page (what a human
reads, in order). For every extractor we measure:
  order      share of consecutive anchor pairs that appear in the right order
             (a missing anchor counts as a failure, e.g. when words are glued)
  rows       table rows / label pairs that stay within 80 characters of each other
  dup_labels how often a diagram callout label is repeated (ground truth: 2)
  seconds    time to extract all 162 pages
The result table is pasted into docs/PARSING_REPORT.md.
"""

from __future__ import annotations

import re
import sys
import time
from collections.abc import Callable

import pdfplumber
import pymupdf
import pypdf

PDF = "data/source/iPhone_User_Guide_1.pdf"

ANCHORS: dict[int, list[str]] = {
    8: ["iPhone at a Glance", "iPhone overview", "This guide describes", "iPhone 5s", "iPhone 5c"],
    12: [
        "Ring/Silent switch",
        "In ring mode",
        "Important:",
        "Status icons",
        "Cell signal",
        "in range of the cellular",
        "Airplane mode",
        "Airplane mode is on",
        "connected to the Internet over a Wi-Fi",
    ],
    22: [
        "Multitasking",
        "View your running apps.",
        "Close an app.",
        "Look around",
        "Get a closer look",
    ],
    126: [
        "Accessibility features",
        "VoiceOver",
        "Widescreen keyboards",
        "Turn on accessibility features.",
        "Use iTunes to configure accessibility.",
    ],
    158: [
        "Turn Cellular Data on or off.",
        "Sell or give away iPhone?",
        "Learn more, service, and support",
        "To learn about",
        "Using iPhone safely",
        "Using other Apple iOS apps",
    ],
    162: [
        "All rights reserved",
        "AirPrint, EarPods",
        "IOS is a trademark",
        "NIKE and the Swoosh",
        "Bluetooth",
        "019-2617/2014-03",
    ],
}

# (page, left, right): the right text must follow the left within 80 characters.
ROW_PAIRS = [
    (12, "Airplane mode", "Airplane mode is on"),
    (12, "EDGE", "EDGE (GSM) network"),
    (12, "Wi-Fi", "connected to the Internet over a Wi-Fi"),
    (158, "Using iCloud", "help.apple.com/icloud"),
    (158, "Managing your Apple ID account", "appleid.apple.com"),
]


def norm(text: str) -> str:
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"')
    text = text.replace("”", '"')
    return re.sub(r"[\s   ]+", " ", text)


def order_score(text: str, anchors: list[str]) -> tuple[int, int]:
    """Anchors found in reading order: each one is searched after the previous match."""
    ok, pos = 0, 0
    for anchor in anchors:
        j = text.find(anchor, pos)
        if j >= 0:
            ok, pos = ok + 1, j + len(anchor)
    return ok, len(anchors)


def row_ok(text: str, left: str, right: str) -> bool:
    i = text.find(left)
    while i >= 0:
        j = text.find(right, i + len(left))
        if 0 <= j - (i + len(left)) <= 80:
            return True
        i = text.find(left, i + 1)
    return False


def pymupdf_native(path: str) -> list[str]:
    with pymupdf.open(path) as doc:
        return [p.get_text() for p in doc]


def pymupdf_sorted(path: str) -> list[str]:
    with pymupdf.open(path) as doc:
        return [p.get_text(sort=True) for p in doc]


def plumber_default(path: str) -> list[str]:
    with pdfplumber.open(path) as pdf:
        return [p.extract_text() or "" for p in pdf.pages]


def plumber_layout(path: str) -> list[str]:
    with pdfplumber.open(path) as pdf:
        return [p.extract_text(layout=True) or "" for p in pdf.pages]


def pypdf_plain(path: str) -> list[str]:
    return [p.extract_text() or "" for p in pypdf.PdfReader(path).pages]


def pypdf_layout(path: str) -> list[str]:
    return [p.extract_text(extraction_mode="layout") or "" for p in pypdf.PdfReader(path).pages]


EXTRACTORS: dict[str, Callable[[str], list[str]]] = {
    "PyMuPDF (native order)": pymupdf_native,
    "PyMuPDF (sort=True)": pymupdf_sorted,
    "pdfplumber (default)": plumber_default,
    "pdfplumber (layout=True)": plumber_layout,
    "pypdf (plain)": pypdf_plain,
    "pypdf (layout)": pypdf_layout,
}


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else PDF
    print("| Extractor | Order (pairs) | Per page | Rows kept | Dup labels p8 | Seconds |")
    print("|---|---|---|---|---|---|")
    for name, fn in EXTRACTORS.items():
        t0 = time.perf_counter()
        pages = [norm(t) for t in fn(path)]
        secs = time.perf_counter() - t0
        per_page, ok_sum, tot_sum = [], 0, 0
        for pno, anchors in ANCHORS.items():
            ok, tot = order_score(pages[pno - 1], anchors)
            ok_sum, tot_sum = ok_sum + ok, tot_sum + tot
            per_page.append(f"p{pno} {ok}/{tot}")
        rows = sum(row_ok(pages[p - 1], a, b) for p, a, b in ROW_PAIRS)
        dups = pages[7].count("Receiver/")
        print(
            f"| {name} | {ok_sum}/{tot_sum} | {', '.join(per_page)} | {rows}/{len(ROW_PAIRS)} "
            f"| {dups} | {secs:.1f} |"
        )


if __name__ == "__main__":
    main()
