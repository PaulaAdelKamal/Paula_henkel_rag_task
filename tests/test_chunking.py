"""Tests for ingestion/chunking.py and ingestion/figures.py on the committed parsed data."""

import json
from dataclasses import replace

import pytest

from ingestion.chunking import (
    BERT_MAX_TOKENS,
    GRID,
    PREFIX_ALLOWANCE,
    RETRIEVABLE,
    ChunkConfig,
    build_chunks,
    count_tokens,
    load_inputs,
)

DEFAULT = ChunkConfig()


@pytest.fixture(scope="module")
def default_chunks():
    return build_chunks(DEFAULT)[1]


@pytest.fixture(scope="module")
def grid():
    return {cfg: build_chunks(cfg)[1] for cfg in GRID}


@pytest.fixture(scope="module")
def pages():
    return {p["page"]: p for p in load_inputs()[0]}


def chunk_containing(chunks, text):
    return next(c for c in chunks if text in c.text)


def element_type(pages, ref):
    page, index = ref
    return pages[page]["elements"][index]["type"]


# --- hard constraints ----------------------------------------------------------------


def test_structure_chunks_never_cross_a_chapter(grid, pages):
    for cfg, chunks in grid.items():
        if cfg.strategy != "structure":
            continue
        for c in chunks:
            chapters = {pages[pg]["chapter_number"] for pg, _ in c.element_refs}
            assert len(chapters) == 1, f"{cfg}: chunk on pages {c.pages} spans {chapters}"


def test_every_chunk_has_page_and_section(default_chunks):
    for c in default_chunks:
        assert c.page == c.page_start == c.pages[0] and c.page_end == c.pages[-1]
        assert 8 <= c.page_start <= c.page_end <= 162
        assert c.pdf_page_index == c.page - 1
        if c.content_type != "legal":
            assert c.chapter_title and c.section_path[0].endswith(c.chapter_title)
            assert c.header.startswith("iPhone User Guide > " + c.section_path[0])


def test_toc_and_front_matter_are_not_chunked(grid):
    for chunks in grid.values():
        assert all(pg >= 8 for c in chunks for pg in c.pages)


def test_every_element_lands_in_a_chunk(grid, pages):
    expected = {
        (p["page"], i)
        for p in pages.values()
        if p["content_type"] in RETRIEVABLE
        for i, _ in enumerate(p["elements"])
    }
    # Repeated column headers of a table continued on the next page are skipped on purpose.
    repeated_headers = {(13, 0), (159, 0)}
    for cfg, chunks in grid.items():
        covered = {tuple(r) for c in chunks for r in c.element_refs}
        wanted = expected
        if cfg.figure_mode == "none":
            wanted = {r for r in expected if element_type(pages, r) != "figure_labels"}
        missing = wanted - covered
        if cfg.strategy == "structure":
            missing -= repeated_headers
        assert not missing, f"{cfg}: {sorted(missing)[:5]}"


def test_token_counts_fit_the_strictest_embedding_model(grid):
    for cfg, chunks in grid.items():
        assert cfg.size + PREFIX_ALLOWANCE <= BERT_MAX_TOKENS
        assert max(c.embed_token_count for c in chunks) <= cfg.size, cfg


def test_chunk_ids_are_deterministic_and_unique(default_chunks):
    again = build_chunks(DEFAULT)[1]
    assert [c.chunk_id for c in again] == [c.chunk_id for c in default_chunks]
    assert len({c.chunk_id for c in default_chunks}) == len(default_chunks)


def test_config_hash_changes_with_the_config():
    assert DEFAULT.config_hash("1.0.0") != replace(DEFAULT, size=200).config_hash("1.0.0")
    assert DEFAULT.config_hash("1.0.0") != DEFAULT.config_hash("1.0.1")  # parser version


# --- overlap ---------------------------------------------------------------------------


def _shared_per_boundary(chunks, pages):
    """Body elements repeated between consecutive chunks (table headers excluded)."""
    out = []
    for a, b in zip(chunks, chunks[1:], strict=False):
        if a.partial and b.partial:
            continue  # two pieces of one oversized element share it by construction
        common = {tuple(r) for r in a.element_refs} & {tuple(r) for r in b.element_refs}
        out.append([r for r in common if element_type(pages, r) != "table_header"])
    return out


def test_oversized_elements_are_split_only_at_the_smallest_size(grid):
    for cfg, chunks in grid.items():
        partial = [c for c in chunks if c.partial]
        if cfg.strategy == "structure" and cfg.size >= 350:
            assert not partial, cfg
        assert all(c.embed_token_count <= cfg.size for c in partial)


def test_no_overlap_means_no_repeated_elements(default_chunks, pages):
    assert not any(_shared_per_boundary(default_chunks, pages))


@pytest.mark.parametrize("size", [200, 350, 500])
def test_overlap_repeats_elements_within_budget(grid, pages, size):
    cfg = ChunkConfig(size=size, overlap=round(size * 0.15))
    boundaries = _shared_per_boundary(grid[cfg], pages)
    assert any(boundaries), "overlap should repeat elements at size splits"
    for shared in boundaries:
        tokens = sum(count_tokens(pages[pg]["elements"][i]["text"]) for pg, i in shared)
        assert tokens <= cfg.overlap


# --- structure-aware behaviour ------------------------------------------------------------


def test_task_section_is_one_chunk_with_its_figure(default_chunks):
    c = chunk_containing(default_chunks, "Close an app.")
    assert c.section_path == ["Chapter 3: Basics", "Use apps", "Multitasking"]
    assert c.lead_ins == ["View your running apps.", "Close an app."]
    assert c.figure_ids == ["p022-f1"] and c.has_figure
    assert c.header == "iPhone User Guide > Chapter 3: Basics > Use apps > Multitasking"
    assert c.embed_text.startswith(c.header + "\n") and not c.text.startswith(c.header)


def test_header_prefix_can_be_disabled(grid):
    chunks = grid[ChunkConfig(header_prefix=False)]
    assert all(c.embed_text == c.text for c in chunks)


def test_continued_table_repeats_its_header_once(default_chunks):
    rows = [c for c in default_chunks if "Do Not Disturb | " in c.text]
    assert rows and all(c.text.count("Status icon | What it means") == 1 for c in rows)


def test_split_chunks_do_not_end_on_an_intro_line(default_chunks):
    for a, b in zip(default_chunks, default_chunks[1:], strict=False):
        same_unit = a.section_path == b.section_path and a.subsection_titles == b.subsection_titles
        if same_unit:
            assert not a.text.rstrip().endswith(":"), a.text[-80:]


def test_figure_labels_mode(grid):
    with_labels = grid[DEFAULT]
    without = grid[ChunkConfig(figure_mode="none")]
    assert any("Figure labels:" in c.text for c in with_labels)
    assert not any("Figure labels:" in c.text for c in without)


def test_naive_baseline_is_fixed_size_and_mapped_to_pages(grid):
    cfg = next(c for c in grid if c.strategy == "naive")
    chunks = grid[cfg]
    assert all(c.embed_text == c.text for c in chunks)  # no contextual header
    assert all(c.pages and c.element_refs for c in chunks)


# --- figures -------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def figures():
    with open("data/parsed/figures.jsonl", encoding="utf-8") as f:
        return [json.loads(line) for line in f]


def test_figures_are_regions_with_anchors(figures):
    kept = [f for f in figures if f["decision"] == "keep"]
    assert len(kept) == 122
    assert all(f["anchor_text"] for f in kept)
    p8 = [f for f in kept if f["page"] == 8]
    assert [f["anchor_text"] for f in p8] == ["iPhone 5s", "iPhone 5c"]  # 88 slices -> 2 figures
    assert all("Receiver/front microphone" in f["labels"][0] for f in p8)
    assert {f["kind"] for f in figures if f["decision"] == "drop"} == {"icon", "app_icon"}
