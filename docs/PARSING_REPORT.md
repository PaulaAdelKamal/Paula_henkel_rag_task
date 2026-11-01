# Parsing report (Step 1)

How `ingestion/parse_pdf.py` turns `data/source/iPhone_User_Guide_1.pdf` into `data/parsed/pages.jsonl` and `data/parsed/toc.json`, and how well it works.

Reproduce:

```bash
uv run python scripts/pdf_inventory.py data/source/iPhone_User_Guide_1.pdf   # forensics
uv run python -m scripts.compare_extractors                                  # extractor comparison
uv run python -m ingestion.parse_pdf                                         # writes data/parsed/
uv run python -m pytest tests/test_parse_pdf.py                              # 19 parser tests
```

## 1. Document inventory (claims from the brief, re-verified)

| Claim | Result |
|---|---|
| 162 pages, 597.6 × 792 pt, no encryption, no outline, attachments or forms | Confirmed. Producer iLovePDF, creator PDFium. |
| Real text layer (Myriad Set, Helvetica TrueType), so no OCR for body text | Confirmed. No U+FFFD replacement characters anywhere; curly quotes, dashes and © decode correctly. |
| Printed page = PDF page index | Confirmed on every page that prints a number (155 of 162). Tested. |
| TOC on pages 2–7: 32 chapters plus appendices A–D | Confirmed. 36 entries, 212 sections. Three short chapters (Weather, Stocks, Calculator) list no sections. |
| Running footer "Chapter 3 Basics 22" | Confirmed on 118 pages. It's a **footer** (y ≈ 735 pt) even though PyMuPDF emits it first. Chapter openers print only the page number. |
| Blue semibold headings, bold run-in lead-ins | Confirmed. The hierarchy is in §3. |
| ~154 cross-references "See … on page N" | **165** found. The first regex (Step 0) found 130 because it missed lowercase "(see … on page N)". |
| ~189 raster image objects, ~155 real plus soft masks, on ~81 pages, max 9 per page | 149 unique images plus 28 soft-mask objects. Counted per page: 153 image occurrences plus 34 soft-mask occurrences = **187**, matching `pdfimages -list`. 81 pages reference image objects (87 including inline images), max 9 per page. Object 247 appears on pp. 8 and 21. |
| No printed captions; diagram callout labels are in the text layer in scrambled order | Confirmed, with a twist: every callout label is drawn **twice** (white halo plus dark copy, offset 0.3 pt). |
| No link annotations | Confirmed: iLovePDF removed them. Cross-references can only come from text. |

## 2. Extractor comparison

Hard pages: 8 (diagram), 12 (status-icon table), 22 (headings and lead-ins), 126 (appendix opener, list of links), 158 (table "To learn about / Do this"), 162 (two-column legal page). For each page, anchors were written by hand from the rendered page: the strings a human reads, in reading order.

| Extractor | Anchors in order | Table rows kept together | Diagram label copies (truth: 2) | Seconds (162 pages) |
|---|---|---|---|---|
| **PyMuPDF, native order** | **36/36** | **5/5** | 4 (whole strings, removable by colour) | **0.5** |
| PyMuPDF, `sort=True` | 34/36 (p162 columns interleaved) | 5/5 | 4 | 2.2 |
| pdfplumber, default | 34/36 (p162 columns interleaved) | 5/5 | 0: labels garbled into "FFaacceeTTiimmee" | 9.0 |
| pdfplumber, `layout=True` | 34/36 | 5/5 | 0: garbled | 10.4 |
| pypdf, plain | 36/36 | 5/5 | 4 | 4.2 |
| pypdf, layout mode | 34/36 | 5/5 | 2 | 3.9 |

PyMuPDF's `find_tables()` was also tried on pages 12 and 13. It loses inter-word spaces ("Whatitmeans") and invents seven columns, so it was rejected.

**Decision: PyMuPDF in native content order** (ADR-0009).
- It ties pypdf on reading order and is 8× faster.
- Crucially, it exposes per-span font, size, colour, flags and bounding boxes. Every structural rule below depends on those.
- pypdf (BSD) is the fallback if PyMuPDF's AGPL licence ever became a problem. Its reading order is identical, but it would need a custom visitor to recover styles.

## 3. Design

**Typography decides the role of every line** (`_line_role`):

| Role | Rule | Example |
|---|---|---|
| footer | y > 720 pt; parsed with a regex, then stripped | `Chapter 3 Basics 22` |
| chapter number | ≥ 40 pt; ignored (the TOC supplies it) | `3` |
| chapter title | ≥ 18 pt | `Basics` |
| section | Myriad Medium 14 pt, blue `#007ac2` | `Use apps` |
| subsection | Myriad Semibold 12 pt, blue | `Multitasking` |
| lead-in | Semibold 10 pt at the start of a paragraph | `Close an app.` |
| callout label | non-Myriad (Helvetica) ≤ 8 pt; white halo copies dropped | `Receiver/ front microphone` |
| table header | white text on the grey bar | `Status icon` · `What it means` |
| list item | line starting with `•` (Myriad and Symbol bullets overlap) | |
| note | italic `Important:` / `WARNING:` (blue) or `Note:` (dark) | |

**Section map.**
- The printed TOC is the ground truth for chapters and page ranges.
- Every one of the 212 TOC sections is found as a section heading on its TOC page, and all 36 chapter titles match their opener pages (tested).
- The footer is harvested as a second source, and it agrees with the TOC on all 118 footer pages (tested).
- Section and subsection context carries across pages: each page records `section_path_start`, and each element records its own `section_path`.

**Elements in reading order.** The parser emits one element per heading, paragraph, list item, table row or figure. Rules that needed geometry:
- **Table rows** (pp. 12, 13, 158, 159): each column is a separate text line, and a wrapped cell can be a separate block. Rows are rebuilt by vertical overlap, cells are assigned to the header's column x-positions, and a new row starts only with first-column text. That keeps the three-paragraph Bluetooth cell on p. 13 in one row.
- **Wrapped bullets and inline icons:** a fragment continues the previous item when it's indented to the bullet's text column right below it (p. 19), or when it starts on the previous element's last baseline because an inline icon split the line (p. 20).
- **Diagram labels:** wrapped label lines are joined by line pitch (8.8 pt within a label, ≥ 16 pt between labels), and labels are ordered column by column. P. 8 now reads "Figure labels: FaceTime camera; Receiver/front microphone; …; Status bar; iSight camera; …; LED flash", once for the iPhone 5s and once for the 5c.
- **Cleaning:** NFKC, then thin and narrow no-break spaces and tabs collapse to single spaces, then the space left before punctuation by a removed inline icon is dropped. Screenshot status-bar text ("9:41 AM", "100%") and the Apple-logo glyph are discarded.

**Cross-references.**
- 165 "see … on page N" references. 164 resolve to the section heading or chapter on the target page; the one miss is "see the following section and Ring/Silent switch on page 12", where the label regex over-captures.
- Another 39 blue link-styled titles without page numbers (e.g. the 21 features listed on p. 126) are resolved through the heading index.

**Content types.**

| Type | Pages | Retrieval |
|---|---|---|
| `front_matter` | 1 | excluded |
| `toc` | 2–7 | excluded |
| `chapter_opener` | 36 | included |
| `content` | 118 | included |
| `legal` | 162 | included, tagged |

The TOC would pollute retrieval: every query about "Mail" would hit the TOC line "Chapter 6: Mail". It's kept as structured data in `toc.json` instead (ADR-0010).

## 4. Output statistics

| Metric | Value |
|---|---|
| Retrievable pages | 155 (265,826 characters, 45,501 words) |
| Characters per page | median 1,711, min 257 (p. 21, chapter opener), max 3,626 |
| Elements | 1,321 paragraphs · 540 list items · 212 sections · 72 subsections · 38 chapter titles · 74 figure-label groups · 34 table rows · 4 table headers |
| Lead-ins | 806 |
| Notes | 43 Note · 19 Important · 12 WARNING |
| Cross-references | 204 (165 by page number, 39 by title), 203 resolved to a target section |
| Pages with tables / figure labels / images | 4 / 63 / 87 |
| Multi-column body text | p. 162 only (columns kept in order) |

`pages.jsonl` and `toc.json` are committed (1.1 MB) so reviewers can inspect them without running anything. They are deterministic: no timestamps, and the parser version and source SHA-256 are in `toc.json`.

## 5. Visual spot check (15 random pages)

Pages were sampled with a fixed seed (20261101) from all non-TOC pages: **19, 20, 23, 31, 37, 40, 42, 44, 64, 78, 92, 108, 122, 133, 151**. Each was rendered and compared with its parsed elements.

| # | Page(s) | Discrepancy | Status |
|---|---|---|---|
| 1 | 19, 20 | Wrapped bullets split into a list item plus a stray paragraph ("your computer. To temporarily…") | **Fixed** (continuation rule, tested) |
| 2 | 20, 64 | An inline icon split a bullet into two fragments ("…Home screen: Tap" + ", then tap…") | **Fixed** (same-baseline rule, tested) |
| 3 | 31, 40, 42 | "Note:" paragraphs not tagged (dark italic, unlike the blue "Important:") | **Fixed** |
| 4 | 19 | A standalone bold line ("Tips for syncing with iTunes on your computer") is kept as the paragraph's lead-in, not as a separate heading | Accepted: the text and order are correct |
| 5 | 78, 108 | Callout labels come column by column, not strictly top to bottom (e.g. bottom-left "View the photos…" listed second) | Accepted: every label present, nothing garbled |
| 6 | 31, 42, 122 | Text inside screenshots (notification list, "Show me great animated films", workout menu) is raster, so it isn't extracted | Step 2 (OCR / vision captions experiment) |
| 7 | 40, 44, 64 | Inline icon glyphs are dropped: "then tap ✈" becomes "then tap." | Known limitation (see below) |

All other content on the 15 pages matched the rendered page: text, reading order, headings, section paths, lead-ins, lists and tables.

## 6. Known limitations

- **Inline icons** aren't text, so instructions like "tap ✈" lose the icon's name. The PDF has 279 inline (non-XObject) image placements, 126 of them icon-sized (< 20 pt), such as ✈, ⓘ and 🔔. Mapping icons to names would need image recognition. That's a Step 2 candidate if evaluation shows it matters.
- **Raster screenshot text** isn't extracted (Step 2).
- **Callout label order** is column-major, an approximation of how a reader scans a callout diagram.
- **One cross-reference label** over-captures (p. 12, "the following section and Ring/Silent switch"); its target page is still correct.
- **Standalone bold lines** are treated as lead-ins of the following paragraph rather than as headings (p. 19); harmless for retrieval.
- **The rules are specific to this guide's typography** (fonts, sizes, the blue `#007ac2`). That's deliberate for a single-document system. A different document would need its own rules, but `scripts/compare_extractors.py` and the tests show how to validate them.
