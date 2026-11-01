"""Parse the iPhone User Guide into structured pages.

Usage: uv run python -m ingestion.parse_pdf [--pdf PATH] [--out DIR]

Writes data/parsed/pages.jsonl (one JSON object per PDF page) and
data/parsed/toc.json. See docs/PARSING_REPORT.md for the design and the
evidence behind each rule.

Approach: PyMuPDF spans in the PDF's native content order (it scored best on
the hard pages, see scripts/compare_extractors.py). Each line is classified by
its typography, which is very regular in this guide:

  footer        y > 720pt, "Chapter N  Title  page" -> stripped, harvested as metadata
  chapter title Semibold 20pt (the 55pt chapter number is ignored)
  section       Medium 14pt blue      -> matches a TOC entry
  subsection    Semibold 12pt blue    e.g. "Multitasking"
  lead-in       Semibold 10pt at the start of a paragraph, e.g. "Close an app."
  callout label Helvetica <= 7.5pt inside figures (printed twice: white halo + dark)
  table header  white Myriad text on a grey bar
  bullet        Symbol-font "•"

The TOC (pages 2-7) is the ground truth for the chapter/section map.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pymupdf

PARSER_VERSION = "1.0.0"
DEFAULT_PDF = Path("data/source/iPhone_User_Guide_1.pdf")
DEFAULT_OUT = Path("data/parsed")

BLUE = 0x007AC2
WHITE = 0xFFFFFF
FOOTER_Y = 720.0
TOC_PAGES = range(2, 8)  # 1-based PDF pages
COVER_PAGES = {1}

_WS = re.compile(r"[\s   ]+")
_TOC_CHAPTER = re.compile(r"^(\d+) (?:Chapter (\d+)|Appendix ([A-D])): ?(.+)$")
_TOC_SECTION = re.compile(r"^(\d+) (.+)$")
_FOOTER = re.compile(r"^(?:(?:Chapter (\d+)|Appendix ([A-D])) (.+?) )?(\d{1,3})$")
_XREF = re.compile(
    r"\bsee (?:also )?(?:the )?[\"“]?"  # "See", "see also", "see the", optional quote
    r"(?P<label>[^.;()\"“”]{2,90}?)[\"”]?,?"  # the target title
    r" on page (?P<page>\d{1,3})",
    re.IGNORECASE,
)
_XREF_PAGE_ONLY = re.compile(r"\bsee page (?P<page>\d{1,3})", re.IGNORECASE)
_CHAPTER_REF = re.compile(r"^Chapter (\d+),")
# Screenshot status-bar text ("9:41 AM", "100%") is not a callout label.
_SCREEN_NOISE = re.compile(r"^[\d:%\s]*(?:AM|PM)?[\d:%\s]*$")


_SPACE_BEFORE_PUNCT = re.compile(r" ([.,;:!?)])")


def clean(text: str) -> str:
    """NFKC + collapse the thin/narrow no-break spaces and tabs the PDF uses.

    Inline icon glyphs (e.g. the bell in "silent mode 🔕.") are not text, so
    removing them leaves "mode ." -- the space before punctuation is dropped.
    """
    text = _WS.sub(" ", unicodedata.normalize("NFKC", text)).strip()
    return _SPACE_BEFORE_PUNCT.sub(r"\1", text)


def title_key(text: str) -> str:
    """Normalised form of a title for matching headings, TOC entries and link texts."""
    return clean(text).casefold().rstrip(" .:")


# --------------------------------------------------------------------------- data model


@dataclass
class TocSection:
    """A section entry of the printed table of contents."""

    title: str
    page: int


@dataclass
class TocChapter:
    """A chapter or appendix of the printed TOC with its page range and sections."""

    chapter_number: str  # "1".."32" or "A".."D"
    chapter_title: str
    kind: str  # "chapter" | "appendix"
    page_start: int
    page_end: int = 0
    sections: list[TocSection] = field(default_factory=list)

    @property
    def label(self) -> str:
        """ "Chapter 3: Basics" or "Appendix A: Accessibility"."""
        prefix = "Chapter" if self.kind == "chapter" else "Appendix"
        return f"{prefix} {self.chapter_number}: {self.chapter_title}"


@dataclass
class Element:
    # chapter_title | section | subsection | paragraph | list_item | table_header
    # | table_row | figure_labels
    """One unit of page content in reading order (heading, paragraph, row, labels…)."""

    type: str
    text: str
    bbox: list[float]
    section_path: list[str]
    lead_in: str | None = None
    note: str | None = None  # "Important" | "WARNING" when the paragraph is a note
    cells: list[str] | None = None  # table rows / header only
    # Internal geometry [x0, y0, y1, text] per line, used to rebuild table rows; not serialised.
    lines: list[list] | None = None


@dataclass
class Xref:
    """A resolved cross-reference ('See X on page N' or a link-styled title)."""

    label: str
    target_page: int
    resolved_by: str  # "page_number" | "title"
    target_section: str | None = None


@dataclass
class Page:
    """Everything parsed from one PDF page; one line of pages.jsonl."""

    pdf_page_index: int  # 0-based index in the PDF file
    page: int  # page number used in citations (== printed page number)
    printed_page: int | None  # number read from the footer (None if the page has no footer)
    content_type: str  # front_matter | toc | chapter_opener | content | legal
    chapter_number: str | None
    chapter_title: str | None
    footer_chapter_number: str | None
    footer_chapter_title: str | None
    section_path_start: list[str]
    elements: list[Element]
    text: str
    headings: list[str]
    xrefs: list[Xref]
    images: list[dict]
    flags: dict[str, bool]
    char_count: int


# --------------------------------------------------------------------------- span helpers


def _is_myriad(span: dict) -> bool:
    """Body fonts are Myriad; diagram callouts use Helvetica."""
    return "Myriad" in span["font"]


def _is_callout(span: dict) -> bool:
    """Small non-Myriad text inside figures (diagram callout labels)."""
    return not _is_myriad(span) and "Symbol" not in span["font"] and span["size"] <= 8.0


def _line_text(line: dict) -> str:
    """Text of a line without the Apple-logo glyph font."""
    return "".join(s["text"] for s in line["spans"] if "AppleIcon" not in s["font"])


def _line_role(line: dict) -> str:
    """Classify a line by typography (footer, heading levels, callout, table header, body…)."""
    spans = [s for s in line["spans"] if s["text"].strip()]
    if not spans:
        return "empty"
    if line["bbox"][1] > FOOTER_Y:
        return "footer"
    if line["dir"] != (1.0, 0.0):
        return "rotated"
    if all(_is_callout(s) for s in spans):
        return "callout_halo" if all(s["color"] == WHITE for s in spans) else "callout"
    if all("AppleIcon" in s["font"] for s in spans) or _line_text(line).strip() == "K":
        return "icon"  # the Apple logo glyph and its fallback "K" (legal page)
    first = spans[0]
    size, font, color = first["size"], first["font"], first["color"]
    if size >= 40:
        return "chapter_number"
    if size >= 18:
        return "chapter_title"
    if color == BLUE and "Medium" in font and 13.0 <= size <= 15.0:
        return "section"
    if color == BLUE and "Semibold" in font and 11.0 <= size <= 13.0:
        return "subsection"
    if all(s["color"] == WHITE for s in spans):
        return "table_header"
    return "body"


def _is_bullet_line(line: dict) -> bool:
    # The PDF draws a Myriad "•" and a Symbol-font "•" on top of each other.
    """Does the line start with a bullet?"""
    return clean(_line_text(line)).startswith("•")


def _lead_in(spans: list[dict]) -> str | None:
    """Bold run-in task title at the start of a paragraph ("Close an app.")."""
    spans = [s for s in spans if s["text"].strip()]
    if not spans:
        return None
    s = spans[0]
    if (
        _is_myriad(s)
        and "Semibold" in s["font"]
        and "Italic" not in s["font"]
        and 9.5 <= s["size"] <= 10.5
        and s["color"] != BLUE
    ):
        return clean(s["text"]) or None
    return None


def _note(spans: list[dict]) -> str | None:
    # "Important:" and "WARNING:" are blue italic, "Note:" is dark italic.
    """'Important', 'WARNING' or 'Note' if the paragraph starts with that italic label."""
    spans = [s for s in spans if s["text"].strip()]
    if spans and "Italic" in spans[0]["font"]:
        word = clean(spans[0]["text"]).rstrip(":")
        if word in {"Important", "WARNING", "Note"}:
            return word
    return None


def _union(bboxes: list[list[float]]) -> list[float]:
    """Bounding box enclosing several boxes."""
    return [
        round(min(b[0] for b in bboxes), 1),
        round(min(b[1] for b in bboxes), 1),
        round(max(b[2] for b in bboxes), 1),
        round(max(b[3] for b in bboxes), 1),
    ]


# --------------------------------------------------------------------------- TOC


def _rows_by_y(page: pymupdf.Page, tol: float = 2.5) -> list[str]:
    """Group spans into visual rows (the TOC puts page numbers and titles in separate lines)."""
    spans = []
    for b in page.get_text("dict")["blocks"]:
        for line in b.get("lines", []):
            for s in line["spans"]:
                if s["text"].strip() and s["bbox"][1] < FOOTER_Y:
                    spans.append(s)
    spans.sort(key=lambda s: (round(s["bbox"][1]), s["bbox"][0]))
    rows: list[list[dict]] = []
    for s in spans:
        if rows and abs(rows[-1][0]["bbox"][1] - s["bbox"][1]) <= tol:
            rows[-1].append(s)
        else:
            rows.append([s])
    return [clean(" ".join(s["text"] for s in sorted(r, key=lambda s: s["bbox"][0]))) for r in rows]


def parse_toc(doc: pymupdf.Document) -> list[TocChapter]:
    """Read chapters, appendices and sections (with pages) from the TOC pages 2–7."""
    chapters: list[TocChapter] = []
    for pno in TOC_PAGES:
        for row in _rows_by_y(doc[pno - 1]):
            if m := _TOC_CHAPTER.match(row):
                page, num, letter, title = m.groups()
                chapters.append(
                    TocChapter(
                        chapter_number=num or letter,
                        chapter_title=title.strip(),
                        kind="chapter" if num else "appendix",
                        page_start=int(page),
                    )
                )
            elif (m := _TOC_SECTION.match(row)) and chapters:
                chapters[-1].sections.append(
                    TocSection(title=m.group(2).strip(), page=int(m.group(1)))
                )
    for current, nxt in zip(chapters, chapters[1:], strict=False):
        current.page_end = nxt.page_start - 1
    if chapters:
        # The last appendix runs until the page before the legal page.
        chapters[-1].page_end = doc.page_count - 1
    return chapters


def chapter_for_page(toc: list[TocChapter], page: int) -> TocChapter | None:
    """The TOC chapter whose page range contains a page."""
    for ch in toc:
        if ch.page_start <= page <= ch.page_end:
            return ch
    return None


# --------------------------------------------------------------------------- page parsing


def _parse_footer(lines: list[dict]) -> tuple[int | None, str | None, str | None]:
    """Printed page number and chapter from the running footer, if present."""
    text = clean(" ".join(_line_text(ln) for ln in lines))
    m = _FOOTER.match(text)
    if not m:
        return None, None, None
    num, letter, title, page = m.groups()
    return int(page), (num or letter), (clean(title) if title else None)


ROW_GAP = 3.0  # vertical gap (pt) that separates two table rows


def _assemble_tables(elements: list[Element]) -> list[Element]:
    """Turn the "_cell" fragments after a table header into table_row elements.

    In this PDF each table column is a separate text line, and a wrapped cell can
    even be a separate block, so rows are rebuilt geometrically: lines that
    overlap vertically form one row, and each line goes to the header column
    whose x position is the closest one to its left.
    """
    out: list[Element] = []
    col_x: list[float] = []
    row_lines: list[list] = []
    row_meta: Element | None = None

    def flush_row() -> None:
        """Emit the collected lines of one table row as a table_row element."""
        nonlocal row_lines, row_meta
        if row_lines and row_meta is not None:
            cells = [""] * max(len(col_x), 1)
            for x0, _y0, _y1, text in sorted(row_lines, key=lambda ln: (ln[1], ln[0])):
                col = max((i for i, x in enumerate(col_x) if x <= x0 + 2), default=0)
                cells[col] = (cells[col] + " " + text).strip()
            cells = [clean(c) for c in cells]
            out.append(
                Element(
                    type="table_row",
                    text=" | ".join(c for c in cells if c),
                    bbox=_union([[ln[0], ln[1], row_meta.bbox[2], ln[2]] for ln in row_lines]),
                    section_path=row_meta.section_path,
                    cells=cells,
                )
            )
        row_lines, row_meta = [], None

    for e in elements:
        if e.type == "table_header":
            flush_row()
            col_x = [ln[0] for ln in (e.lines or [])]
            e.cells = e.cells or [e.text]
            out.append(e)
        elif e.type == "_cell":
            for ln in e.lines or []:
                # A new row starts only with text in the first column; a gap followed by
                # text in a later column is another paragraph of the same cell (p13 Bluetooth).
                first_col = len(col_x) < 2 or ln[0] < col_x[1] - 2
                if row_lines and first_col and ln[1] > max(r[2] for r in row_lines) + ROW_GAP:
                    flush_row()
                if row_meta is None:
                    row_meta = e
                row_lines.append(ln)
        else:
            flush_row()
            out.append(e)
    flush_row()
    return out


LABEL_LINE_PITCH = 11.0  # wrapped lines of one label are 8.8pt apart, labels >= 16pt apart


def _order_labels(labels: list[tuple[list[float], str]]) -> list[str]:
    """Rebuild callout labels and order them column-major (left column top-to-bottom first).

    Wrapped lines of one label ("Receiver/", "front", "microphone") share an x
    position and sit one line pitch apart; a slash ending joins without a space.
    """
    columns: list[list[tuple[list[float], str]]] = []
    for bbox, text in sorted(labels, key=lambda t: t[0][0]):
        if columns and abs(columns[-1][0][0][0] - bbox[0]) <= 20:
            columns[-1].append((bbox, text))
        else:
            columns.append([(bbox, text)])
    out: list[str] = []
    for col in columns:
        prev_y: float | None = None
        for bbox, text in sorted(col, key=lambda t: t[0][1]):
            if prev_y is not None and bbox[1] - prev_y < LABEL_LINE_PITCH:
                out[-1] += ("" if out[-1].endswith("/") else " ") + text
            else:
                out.append(text)
            prev_y = bbox[1]
    return out


LIST_TEXT_X = 113.0  # bullet text starts ~8pt right of the 105pt margin (glyph at 105-110)


def _geometry(lines: list[dict]) -> list[list]:
    """Line geometry kept for table and continuation logic: [x0, y0, y1, text]."""
    return [[ln["bbox"][0], ln["bbox"][1], ln["bbox"][3], clean(_line_text(ln))] for ln in lines]


def _continues_previous(elements: list[Element], lines: list[dict]) -> bool:
    """Is this fragment the rest of the previous list item / paragraph?

    Two layouts split one logical item into several PyMuPDF blocks:
    - a wrapped bullet whose continuation lines form a new block, indented to the
      bullet's text column right below it (p19);
    - an inline icon that splits a visual line in two: the rest of the line starts
      on the same baseline as the previous element's last line (p20 "Tap [icon], then").
    """
    if (
        not elements
        or elements[-1].type not in {"list_item", "paragraph"}
        or not elements[-1].lines
    ):
        return False
    prev_last = elements[-1].lines[-1]  # [x0, y0, y1, text]
    x0, y0 = lines[0]["bbox"][0], lines[0]["bbox"][1]
    same_baseline = abs(y0 - prev_last[1]) < 2 and x0 > prev_last[0]
    wrapped_bullet = (
        elements[-1].type == "list_item"
        and LIST_TEXT_X - 2 <= x0 <= LIST_TEXT_X + 8
        and 0 <= y0 - prev_last[2] < 6
    )
    return same_baseline or wrapped_bullet


class _State:
    """Section context carried across pages."""

    def __init__(self) -> None:
        """No chapter, section or subsection yet."""
        self.chapter: TocChapter | None = None
        self.section: str | None = None
        self.subsection: str | None = None

    def path(self) -> list[str]:
        """Current section path, e.g. [chapter, section, subsection]."""
        out = [self.chapter.label] if self.chapter else []
        return out + [p for p in (self.section, self.subsection) if p]


class _PageBuilder:
    """Turns the classified lines of one page into Elements, in reading order."""

    def __init__(self, state: _State) -> None:
        """Start an empty page; the section state is shared across pages."""
        self.state = state
        self.elements: list[Element] = []
        self.footer_lines: list[dict] = []
        self.labels: list[tuple[list[float], str]] = []
        self.para_lines: list[dict] = []
        self.para_kind = "paragraph"  # "list_item" while the lines belong to a bullet
        self.in_table = False  # set by a table header, cleared by headings and normal text

    def add_block(self, block: dict) -> None:
        """Add every line of a PyMuPDF block, then close the open paragraph."""
        for line in block["lines"]:
            self.add_line(line)
        self.flush_para()  # a paragraph never continues across blocks (see _continues_previous)

    def add_line(self, line: dict) -> None:
        """Route one line by role: skip, footer, callout label, heading or body text."""
        role = _line_role(line)
        if role in {"empty", "rotated", "chapter_number", "callout_halo", "icon"}:
            return
        if role == "footer":
            self.footer_lines.append(line)
            return
        text = clean(_line_text(line))
        if role == "callout":
            if not _SCREEN_NOISE.match(text):
                self.labels.append((list(line["bbox"]), text))
            return
        if role in {"chapter_title", "section", "subsection", "table_header"}:
            self.add_heading(role, text, line)
            return
        # Body line. Callout labels of one figure arrive as consecutive blocks,
        # so they are only flushed once body text resumes.
        self.flush_labels()
        if _is_bullet_line(line):
            self.flush_para()
            self.para_kind = "list_item"
        elif self.para_kind == "list_item" and line["bbox"][0] < LIST_TEXT_X - 2:
            self.flush_para()  # an un-indented line ends the bullet
        self.para_lines.append(line)

    def add_heading(self, role: str, text: str, line: dict) -> None:
        """Close open content, update the section state and emit the heading."""
        self.flush_para()
        self.flush_labels()
        if role == "section":
            self.state.section, self.state.subsection = text, None
        elif role == "subsection":
            self.state.subsection = text
        self.in_table = role == "table_header"
        self.elements.append(
            Element(
                type=role,
                text=text,
                bbox=[round(v, 1) for v in line["bbox"]],
                section_path=self.state.path(),
                lines=_geometry([line]),
            )
        )

    def flush_labels(self) -> None:
        """Emit collected callout labels as one figure_labels element."""
        if not self.labels:
            return
        self.elements.append(
            Element(
                type="figure_labels",
                text="Figure labels: " + "; ".join(_order_labels(self.labels)),
                bbox=_union([bbox for bbox, _ in self.labels]),
                section_path=self.state.path(),
            )
        )
        self.labels = []

    def flush_para(self) -> None:
        """Emit the open paragraph, list item or table-cell fragment (or merge a continuation)."""
        lines, kind = self.para_lines, self.para_kind
        self.para_lines, self.para_kind = [], "paragraph"
        if not lines:
            return
        if _continues_previous(self.elements, lines):
            prev = self.elements[-1]
            prev.text = clean(prev.text + " " + " ".join(_line_text(ln) for ln in lines))
            prev.bbox = _union([prev.bbox] + [list(ln["bbox"]) for ln in lines])
            prev.lines = (prev.lines or []) + _geometry(lines)
            return
        spans = [s for ln in lines for s in ln["spans"] if s["text"].strip()]
        text = clean(" ".join(_line_text(ln) for ln in lines))
        if kind == "list_item":
            text = text.lstrip("• ").strip()
            spans = [s for s in spans if s["text"].strip() != "•"]
        if not text:
            return
        small = all(s["size"] < 9.5 for s in spans)
        if kind == "paragraph" and small and self.in_table:
            kind = "_cell"  # fragment of a table row, rebuilt by _assemble_tables
        elif not small:
            self.in_table = False
        self.elements.append(
            Element(
                type=kind,
                text=text,
                bbox=_union([list(ln["bbox"]) for ln in lines]),
                section_path=self.state.path(),
                lead_in=_lead_in(spans) if kind != "_cell" else None,
                note=_note(spans) if kind != "_cell" else None,
                lines=_geometry(lines),
            )
        )


def _content_type(page_no: int, page_count: int, toc: list[TocChapter]) -> str:
    """front_matter, toc, legal, chapter_opener or content, from the page number."""
    if page_no in COVER_PAGES:
        return "front_matter"
    if page_no in TOC_PAGES:
        return "toc"
    if page_no == page_count:
        return "legal"
    if any(ch.page_start == page_no for ch in toc):
        return "chapter_opener"
    return "content"


def parse_page(doc: pymupdf.Document, index: int, toc: list[TocChapter], state: _State) -> Page:
    """Parse one page into elements, footer metadata, images and flags."""
    page_obj = doc[index]
    page_no = index + 1
    content_type = _content_type(page_no, doc.page_count, toc)
    in_chapter = content_type in {"content", "chapter_opener"}
    chapter = chapter_for_page(toc, page_no) if in_chapter else None
    if chapter is not state.chapter:
        state.chapter, state.section, state.subsection = chapter, None, None
    section_path_start = state.path()

    builder = _PageBuilder(state)
    for block in page_obj.get_text("dict")["blocks"]:
        if block.get("type") == 0:
            builder.add_block(block)
    builder.flush_labels()
    elements, footer_lines = builder.elements, builder.footer_lines

    _merge_label_blocks(elements)
    elements = _assemble_tables(elements)

    printed, footer_ch, footer_title = _parse_footer(footer_lines)
    if content_type == "legal" and state.chapter is None:
        section_path_start = []

    text = "\n".join(e.text for e in elements)
    headings = [e.text for e in elements if e.type in {"chapter_title", "section", "subsection"}]
    images = [
        {"xref": info.get("xref"), "bbox": [round(v, 1) for v in info["bbox"]]}
        for info in page_obj.get_image_info(xrefs=True)
    ]
    flags = {
        "has_table": any(e.type in {"table_row", "table_header"} for e in elements),
        "has_figure_labels": any(e.type == "figure_labels" for e in elements),
        "has_images": bool(images),
        "multi_column": any(
            e.type in {"paragraph", "list_item"} and e.bbox[0] > 250 for e in elements
        ),
    }
    return Page(
        pdf_page_index=index,
        page=page_no,
        printed_page=printed,
        content_type=content_type,
        chapter_number=chapter.chapter_number if chapter else None,
        chapter_title=chapter.chapter_title if chapter else None,
        footer_chapter_number=footer_ch,
        footer_chapter_title=footer_title,
        section_path_start=section_path_start,
        elements=elements,
        text=text,
        headings=headings,
        xrefs=[],
        images=images,
        flags=flags,
        char_count=len(text),
    )


def _merge_label_blocks(elements: list[Element]) -> None:
    """Adjacent figure_labels belong to one figure; adjacent header cells to one header row."""
    i = 0
    while i < len(elements) - 1:
        a, b = elements[i], elements[i + 1]
        if a.type == b.type == "figure_labels":
            a.text = a.text + "; " + b.text.removeprefix("Figure labels: ")
            a.bbox = _union([a.bbox, b.bbox])
            del elements[i + 1]
        elif a.type == b.type == "table_header" and abs(a.bbox[1] - b.bbox[1]) < 3:
            a.cells = (a.cells or [a.text]) + [b.text]
            a.text = " | ".join(a.cells)
            a.bbox = _union([a.bbox, b.bbox])
            a.lines = (a.lines or []) + (b.lines or [])
            del elements[i + 1]
        else:
            i += 1


# --------------------------------------------------------------------------- cross-references


def heading_index(pages: list[Page], toc: list[TocChapter]) -> dict[str, tuple[int, str]]:
    """title -> (page, section path) for every section/subsection heading and TOC entry."""
    index: dict[str, tuple[int, str]] = {}
    for ch in toc:
        for sec in ch.sections:
            index.setdefault(title_key(sec.title), (sec.page, f"{ch.label} > {sec.title}"))
    for p in pages:
        for e in p.elements:
            if e.type in {"section", "subsection"}:
                index.setdefault(title_key(e.text), (p.page, " > ".join(e.section_path)))
    return index


def extract_xrefs(text: str) -> list[tuple[str, int]]:
    """All 'see X on page N' and 'see page N' references in a text."""
    refs = [
        (clean(m.group("label")).rstrip(","), int(m.group("page"))) for m in _XREF.finditer(text)
    ]
    refs += [("", int(m.group("page"))) for m in _XREF_PAGE_ONLY.finditer(text)]
    return refs


def _blue_titles(page_obj: pymupdf.Page) -> list[str]:
    """Blue body-text runs (link styling) on a page, merged across adjacent spans."""
    runs: list[str] = []
    current = ""
    for b in page_obj.get_text("dict")["blocks"]:
        for line in b.get("lines", []):
            for s in line["spans"]:
                is_link = s["color"] == BLUE and _is_myriad(s) and 9.5 <= s["size"] <= 10.5
                is_link = is_link and "Italic" not in s["font"] and "Medium" not in s["font"]
                if is_link and "Semibold" not in s["font"]:
                    current += s["text"]
                elif current:
                    runs.append(clean(current))
                    current = ""
    if current:
        runs.append(clean(current))
    return [r for r in runs if r]


def attach_xrefs(doc: pymupdf.Document, pages: list[Page], toc: list[TocChapter]) -> None:
    """Resolve each page's cross-references to target sections via the heading index."""
    index = heading_index(pages, toc)
    for p in pages:
        if p.content_type in {"toc", "front_matter"}:
            continue
        seen: set[tuple[str, int]] = set()
        for label, target in extract_xrefs(p.text):
            if (label, target) in seen or not 1 <= target <= doc.page_count:
                continue
            seen.add((label, target))
            hit = index.get(title_key(label))
            section = hit[1] if hit and hit[0] == target else None
            if section is None and (m := _CHAPTER_REF.match(label)):
                # "See Chapter 4, Siri, on page 41" points at a whole chapter.
                ch = next((c for c in toc if c.chapter_number == m.group(1)), None)
                section = ch.label if ch and ch.page_start == target else None
            p.xrefs.append(
                Xref(
                    label=label,
                    target_page=target,
                    resolved_by="page_number",
                    target_section=section,
                )
            )
        labelled = {title_key(x.label) for x in p.xrefs}
        for title in _blue_titles(doc[p.pdf_page_index]):
            key = title_key(title)
            if key in labelled or key not in index or key in {title_key(h) for h in p.headings}:
                continue
            target, section = index[key]
            if (title, target) in seen:
                continue
            seen.add((title, target))
            p.xrefs.append(
                Xref(label=title, target_page=target, resolved_by="title", target_section=section)
            )


# --------------------------------------------------------------------------- driver


def parse_document(pdf_path: Path = DEFAULT_PDF) -> tuple[list[TocChapter], list[Page]]:
    """Parse the whole PDF: TOC first, then every page in order, then cross-references."""
    with pymupdf.open(pdf_path) as doc:
        toc = parse_toc(doc)
        state = _State()
        pages = [parse_page(doc, i, toc, state) for i in range(doc.page_count)]
        attach_xrefs(doc, pages, toc)
    return toc, pages


def write_outputs(toc: list[TocChapter], pages: list[Page], pdf_path: Path, out_dir: Path) -> None:
    """Write pages.jsonl and toc.json (with source hash and parser version)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "pages.jsonl").open("w", encoding="utf-8", newline="\n") as f:
        for p in pages:
            record = asdict(p)
            for e in record["elements"]:
                e.pop("lines", None)
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    toc_json = {
        "source": pdf_path.name,
        "source_sha256": hashlib.sha256(pdf_path.read_bytes()).hexdigest(),
        "parser_version": PARSER_VERSION,
        "chapters": [asdict(ch) | {"label": ch.label} for ch in toc],
    }
    (out_dir / "toc.json").write_text(
        json.dumps(toc_json, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )


def main() -> None:
    """CLI: parse the PDF and print a summary."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pdf", type=Path, default=DEFAULT_PDF)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()
    toc, pages = parse_document(args.pdf)
    write_outputs(toc, pages, args.pdf, args.out)
    types: dict[str, int] = {}
    for p in pages:
        types[p.content_type] = types.get(p.content_type, 0) + 1
    print(f"pages={len(pages)} chapters={len(toc)} sections={sum(len(c.sections) for c in toc)}")
    print(f"content types: {types}")
    n_xrefs = sum(len(p.xrefs) for p in pages)
    print(f"xrefs: {n_xrefs}  elements: {sum(len(p.elements) for p in pages)}")


if __name__ == "__main__":
    main()
