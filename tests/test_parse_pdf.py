"""Tests for ingestion/parse_pdf.py against the real source PDF (parsing takes about a second)."""

import re

import pytest

from ingestion.parse_pdf import clean, extract_xrefs, parse_document

CHAPTER_NUMBERS = [str(n) for n in range(1, 33)] + ["A", "B", "C", "D"]


@pytest.fixture(scope="module")
def parsed():
    return parse_document()


@pytest.fixture(scope="module")
def toc(parsed):
    return parsed[0]


@pytest.fixture(scope="module")
def pages(parsed):
    return parsed[1]


def elements(pages, page, type_=None):
    return [e for e in pages[page - 1].elements if type_ is None or e.type == type_]


# --- page numbers: a citation mismatch would fail the grader's check -------------


def test_printed_page_equals_pdf_page(pages):
    assert len(pages) == 162
    numbered = [p for p in pages if p.printed_page is not None]
    assert len(numbered) >= 150
    for p in pages:
        assert p.page == p.pdf_page_index + 1
        if p.printed_page is not None:
            assert p.printed_page == p.page, f"PDF page {p.page} prints {p.printed_page}"


# --- TOC section map ----------------------------------------------------------------


def test_toc_has_32_chapters_and_4_appendices(toc):
    assert [c.chapter_number for c in toc] == CHAPTER_NUMBERS
    assert toc[0].chapter_title == "iPhone at a Glance" and toc[0].page_start == 8
    assert toc[31].chapter_title == "Podcasts"
    assert [c.page_start for c in toc[32:]] == [126, 147, 149, 151]
    assert all(c.kind == "appendix" for c in toc[32:])


def test_toc_page_ranges_are_contiguous(toc):
    assert toc[0].page_start == 8 and toc[-1].page_end == 161
    for current, nxt in zip(toc, toc[1:], strict=False):
        assert current.page_end == nxt.page_start - 1
    # Three short single-topic chapters list no sections in the printed TOC.
    without_sections = [c.chapter_title for c in toc if not c.sections]
    assert without_sections == ["Weather", "Stocks", "Calculator"]


def test_every_toc_section_is_a_heading_on_its_page(toc, pages):
    for chapter in toc:
        for section in chapter.sections:
            headings = [e.text for e in elements(pages, section.page, "section")]
            assert section.title in headings, f"{section.title!r} not on p{section.page}"


def test_content_types(pages, toc):
    by_type = {}
    for p in pages:
        by_type.setdefault(p.content_type, []).append(p.page)
    assert by_type["front_matter"] == [1]
    assert by_type["toc"] == [2, 3, 4, 5, 6, 7]
    assert by_type["legal"] == [162]
    assert by_type["chapter_opener"] == [c.page_start for c in toc]


# --- running footer: stripped from text, harvested as metadata --------------------------


def test_footer_text_is_removed(pages):
    footer = re.compile(r"^(Chapter \d+|Appendix [A-D]) .+ \d{1,3}$")
    for p in pages:
        for e in p.elements:
            assert not footer.match(e.text), f"footer left on p{p.page}: {e.text!r}"


def test_footer_chapter_agrees_with_toc(pages):
    with_footer = [p for p in pages if p.footer_chapter_number]
    assert len(with_footer) == 118
    for p in with_footer:
        assert p.footer_chapter_number == p.chapter_number
        assert p.footer_chapter_title == p.chapter_title


# --- cross-references ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Pull down the screen. See Search on page 30.", [("Search", 30)]),
        ("(see Find My iPhone on page 39), Activation Lock", [("Find My iPhone", 39)]),
        ("For more, see Chapter 30, iBooks, on page 118.", [("Chapter 30, iBooks", 118)]),
        ("No reference here.", []),
    ],
)
def test_extract_xrefs(text, expected):
    assert extract_xrefs(text) == expected


def test_xrefs_resolve_to_target_sections(pages):
    p22 = {(x.label, x.target_page): x for x in pages[21].xrefs}
    assert p22[("Search", 30)].target_section == "Chapter 3: Basics > Search"
    by_page_number = [x for p in pages for x in p.xrefs if x.resolved_by == "page_number"]
    assert len(by_page_number) >= 150
    resolved = sum(1 for x in by_page_number if x.target_section)
    assert resolved / len(by_page_number) > 0.95


def test_link_styled_titles_resolve_without_page_numbers(pages):
    titles = {x.label: x.target_page for x in pages[125].xrefs if x.resolved_by == "title"}
    assert titles["VoiceOver"] == 127
    assert titles["AssistiveTouch"] == 145


# --- structure: headings, lead-ins, lists, tables, figures ------------------------------


def test_lead_ins_and_section_path(pages):
    close = next(e for e in elements(pages, 22, "paragraph") if e.lead_in == "Close an app.")
    assert close.section_path == ["Chapter 3: Basics", "Use apps", "Multitasking"]
    assert close.text.startswith("Close an app. If an app isn’t working properly")


def test_figure_callouts_are_deduplicated_and_ordered(pages):
    labels = [e.text for e in elements(pages, 8, "figure_labels")]
    assert len(labels) == 2  # iPhone 5s and iPhone 5c
    for text in labels:
        assert text.count("Receiver/front microphone") == 1  # white halo copy dropped
        assert "; Status bar; iSight camera;" in text  # wrapped lines joined per label
    assert "Home button/Touch ID sensor" in labels[0]


def test_table_rows_are_rebuilt(pages):
    rows = [e.cells for e in elements(pages, 12, "table_row")]
    assert ["Airplane mode", rows[1][1]] == rows[1] and rows[1][1].startswith("Airplane mode is on")
    bluetooth = [e for e in elements(pages, 13, "table_row") if e.text.startswith("Bluetooth®")]
    assert len(bluetooth) == 1 and "No icon: Bluetooth is turned off." in bluetooth[0].text
    assert elements(pages, 158, "table_header")[0].cells == ["To learn about", "Do this"]


def test_wrapped_bullets_stay_in_one_list_item(pages):
    items = [e.text for e in elements(pages, 19, "list_item")]
    assert items[0].endswith("until you see iPhone appear in the iTunes window.")
    assert not any(e.text.startswith("your computer.") for e in elements(pages, 19))


def test_notes_are_tagged(pages):
    notes = {e.note for p in pages for e in p.elements if e.note}
    assert notes == {"Important", "WARNING", "Note"}


def test_clean_normalises_pdf_whitespace():
    assert clean("Chapter  3   Basics\t") == "Chapter 3 Basics"
    assert clean("iPhone 5s") == "iPhone 5s"
    assert clean("silent mode .") == "silent mode."
