"""Turn parsed pages into retrieval chunks.

Usage:
  uv run python -m ingestion.chunking                 # default config
  uv run python -m ingestion.chunking --size 200 --overlap 30 --no-header
  uv run python -m ingestion.chunking --grid          # every config of the Step 3 grid

Writes data/chunks/<chunk_config_hash>.jsonl (+ .config.json with stats).

Two strategies, one config object (hashed into `chunk_config_hash`):

structure  Pack whole parsed elements (paragraphs, list items, table rows,
           figure labels) into chunks of at most `size` tokens. Chapters and
           sections are hard boundaries; a subsection boundary closes the chunk
           only if it already holds `min_size` tokens (tiny units merge forward).
           Never ends a chunk on an introductory line ending in ":", and repeats
           the table header when a table continues in a new chunk.
naive      Baseline: LangChain's RecursiveCharacterTextSplitter over the
           concatenated page text, metadata recovered from character offsets.

Token unit: BERT WordPiece (bert-base-uncased vocabulary), the tokenizer of the
strictest embedding candidates (BGE / E5 / GTE, 512-token limit). `size`
bounds the *embedded* text (contextual header + body), so size <= 500 leaves
room for model prefixes ("passage: ") and the two special tokens.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Literal

from langchain_text_splitters import RecursiveCharacterTextSplitter
from tokenizers import BertWordPieceTokenizer

CHUNKER_VERSION = "1.0.0"
SOURCE = "iPhone_User_Guide_1.pdf"
DOC_VERSION = "iOS 7.1 iPhone User Guide, 019-2617/2014-03"  # part number on the last page
DOC_TITLE = "iPhone User Guide"
VOCAB = Path(__file__).parent / "assets" / "bert-base-uncased-vocab.txt"
PAGES = Path("data/parsed/pages.jsonl")
FIGURES = Path("data/parsed/figures.jsonl")
TOC = Path("data/parsed/toc.json")
OUT_DIR = Path("data/chunks")
RETRIEVABLE = {"content", "chapter_opener", "legal"}
HEADING_TYPES = {"chapter_title", "section", "subsection"}
BERT_MAX_TOKENS = 512
PREFIX_ALLOWANCE = 10  # longest document-side prefix of a candidate model + [CLS]/[SEP]
_ID_NAMESPACE = uuid.UUID("6f1d3b5e-2c4a-4f7e-9a51-1b8e0c6d7a90")


@lru_cache(maxsize=1)
def _tokenizer() -> BertWordPieceTokenizer:
    """The BERT WordPiece tokenizer from the committed vocabulary (loaded once)."""
    return BertWordPieceTokenizer(str(VOCAB), lowercase=True)


def count_tokens(text: str) -> int:
    """Number of BERT tokens in a text (the unit of every size limit here)."""
    return len(_tokenizer().encode(text, add_special_tokens=False).ids)


@dataclass(frozen=True)
class ChunkConfig:
    """Every chunking choice; hashed into chunk_config_hash and the collection name."""

    strategy: Literal["structure", "naive"] = "structure"
    size: int = 350  # max tokens of the embedded text (header + body)
    overlap: int = 0  # tokens repeated when a unit is split by size
    header_prefix: bool = True  # prepend "iPhone User Guide > Chapter > Section > Subsection"
    figure_mode: Literal["labels", "none"] = "labels"  # keep diagram callout labels as text?
    min_size: int = 64  # a subsection boundary closes the chunk only above this size

    def config_hash(self, parser_version: str) -> str:
        """Short stable hash of this config plus chunker and parser versions."""
        payload = json.dumps(
            asdict(self) | {"chunker": CHUNKER_VERSION, "parser": parser_version},
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:10]


@dataclass
class Chunk:
    """One retrieval chunk with all the metadata stored in Qdrant."""

    chunk_id: str
    chunk_config_hash: str
    source: str
    doc_version: str
    page: int  # page used for citations (= page_start)
    pdf_page_index: int
    page_start: int
    page_end: int
    pages: list[int]
    chapter_number: str | None
    chapter_title: str | None
    section_title: str | None
    subsection_titles: list[str]
    section_path: list[str]
    content_type: str  # text | table | legal
    lead_ins: list[str]
    has_figure: bool
    figure_ids: list[str]
    xrefs: list[dict]
    header: str
    text: str  # what the UI shows
    embed_text: str  # what gets embedded (header + text when header_prefix)
    char_count: int
    token_count: int  # body tokens
    embed_token_count: int  # header + body tokens
    element_refs: list[list[int]] = field(default_factory=list)  # [page, element index]
    partial: bool = False  # piece of an element longer than `size` (split by the safety net)


@dataclass
class _Item:
    """One parsed element in reading order, with its page, index and token count."""

    page: int
    index: int
    element: dict
    tokens: int


# --------------------------------------------------------------------------- loading


def load_inputs(
    pages_path: Path = PAGES, figures_path: Path = FIGURES, toc_path: Path = TOC
) -> tuple[list[dict], dict[tuple[int, int], list[str]], str]:
    """Read parsed pages, the figure anchors and the parser version."""
    with pages_path.open(encoding="utf-8") as f:
        pages = [json.loads(line) for line in f]
    figures: dict[tuple[int, int], list[str]] = {}
    if figures_path.exists():
        with figures_path.open(encoding="utf-8") as f:
            for line in f:
                fig = json.loads(line)
                if fig["decision"] == "keep" and fig["anchor_element"] is not None:
                    key = (fig["page"], fig["anchor_element"])
                    figures.setdefault(key, []).append(fig["figure_id"])
    parser_version = json.loads(toc_path.read_text(encoding="utf-8"))["parser_version"]
    return pages, figures, parser_version


def _items(pages: list[dict], figure_mode: str) -> list[_Item]:
    """Retrievable elements in reading order (TOC and cover excluded; labels optional)."""
    out = []
    for p in pages:
        if p["content_type"] not in RETRIEVABLE:
            continue
        for i, e in enumerate(p["elements"]):
            if figure_mode == "none" and e["type"] == "figure_labels":
                continue
            out.append(_Item(p["page"], i, e, count_tokens(e["text"])))
    return out


# --------------------------------------------------------------------------- shared assembly


def _header(path: list[str], subsections: list[str]) -> str:
    """Contextual header: guide > chapter > section > subsection(s)."""
    parts = [DOC_TITLE, *path[:2]]
    if subsections:
        parts.append(", ".join(subsections))
    return " > ".join(parts)


def _make_chunk(
    items: list[_Item],
    cfg: ChunkConfig,
    cfg_hash: str,
    pages_by_no: dict[int, dict],
    figures: dict[tuple[int, int], list[str]],
    text: str | None = None,
    partial: bool = False,
) -> Chunk:
    """Assemble a Chunk from items.

    Sets text, header, pages, sections, figures, cross-references and a deterministic id.
    """
    body_items = [it for it in items if it.element["type"] not in HEADING_TYPES] or items
    first = body_items[0].element
    path = first["section_path"]
    subsections: list[str] = []
    for it in items:
        sp = it.element["section_path"]
        if len(sp) > 2 and sp[2] not in subsections:
            subsections.append(sp[2])
    header = _header(path, subsections) if path else DOC_TITLE
    body = text if text is not None else "\n".join(it.element["text"] for it in items)
    embed = f"{header}\n{body}" if cfg.header_prefix else body
    page_list = sorted({it.page for it in items})
    first_page = pages_by_no[page_list[0]]
    xrefs = [
        x for pg in page_list for x in pages_by_no[pg]["xrefs"] if x["label"] and x["label"] in body
    ]
    figure_ids = [fid for it in items for fid in figures.get((it.page, it.index), [])]
    n_rows = sum(1 for it in items if it.element["type"] == "table_row")
    if first_page["content_type"] == "legal":
        content_type = "legal"
    elif n_rows and n_rows >= len(body_items) / 2:
        content_type = "table"
    else:
        content_type = "text"
    key = f"{cfg_hash}|{page_list[0]}|{hashlib.sha256(embed.encode()).hexdigest()}"
    return Chunk(
        chunk_id=str(uuid.uuid5(_ID_NAMESPACE, key)),
        chunk_config_hash=cfg_hash,
        source=SOURCE,
        doc_version=DOC_VERSION,
        page=page_list[0],
        pdf_page_index=page_list[0] - 1,
        page_start=page_list[0],
        page_end=page_list[-1],
        pages=page_list,
        chapter_number=first_page["chapter_number"],
        chapter_title=first_page["chapter_title"],
        section_title=path[1] if len(path) > 1 else None,
        subsection_titles=subsections,
        section_path=path,
        content_type=content_type,
        lead_ins=[it.element["lead_in"] for it in items if it.element.get("lead_in")],
        has_figure=bool(figure_ids),
        figure_ids=figure_ids,
        xrefs=xrefs,
        header=header,
        text=body,
        embed_text=embed,
        char_count=len(body),
        token_count=count_tokens(body),
        embed_token_count=count_tokens(embed),
        element_refs=[[it.page, it.index] for it in items],
        partial=partial,
    )


# --------------------------------------------------------------------------- structure strategy


def _chapter(it: _Item) -> str | None:
    """Chapter label of an item (first entry of its section path)."""
    sp = it.element["section_path"]
    return sp[0] if sp else None


def _section(it: _Item) -> tuple[str, ...]:
    """Chapter + section of an item (the hard chunk boundary)."""
    return tuple(it.element["section_path"][:2])


def chunk_structure(
    pages: list[dict], figures: dict[tuple[int, int], list[str]], cfg: ChunkConfig, cfg_hash: str
) -> list[Chunk]:
    """Structure-aware chunking: pack whole elements within section boundaries."""
    pages_by_no = {p["page"]: p for p in pages}
    groups: list[list[_Item] | Chunk] = []  # Chunk = pre-built pieces of an oversized element
    started_by_size: list[bool] = []  # was this group opened because the previous one was full?
    current: list[_Item] = []
    current_by_size = False
    table_header: _Item | None = None

    def tokens_with(items: list[_Item]) -> int:
        """Embedded size estimate: body tokens + newlines + header tokens."""
        body = sum(it.tokens for it in items) + max(len(items) - 1, 0)
        if not cfg.header_prefix or not items:
            return body
        return body + count_tokens(_header(items[-1].element["section_path"], [])) + 4

    def close(by_size: bool) -> None:
        """Emit the current group. Size splits may carry an intro line and overlap forward;
        structural boundaries never carry anything across."""
        nonlocal current, current_by_size
        if not _has_body(current):
            return  # headings alone never form a chunk; they wait for their content
        carry: list[_Item] = []
        if by_size:
            # Never end on an intro line ("...set the following:"); move it forward.
            while len(current) > 1 and current[-1].element["text"].rstrip().endswith(":"):
                carry.insert(0, current.pop())
            if cfg.overlap:
                tail: list[_Item] = []
                for it in reversed(current):
                    if it.element["type"] in HEADING_TYPES:
                        break
                    if sum(t.tokens for t in tail) + it.tokens > cfg.overlap:
                        break
                    tail.insert(0, it)
                carry = tail + carry
        groups.append(current)
        started_by_size.append(current_by_size)
        current, current_by_size = carry, by_size

    for it in _items(pages, cfg.figure_mode):
        etype = it.element["type"]
        if current:
            hard_boundary = (
                _chapter(it) != _chapter(current[-1])
                or etype in {"chapter_title", "section"}
                or (_section(it) != _section(current[-1]) and _has_body(current))
            )
            # A subsection starts a new chunk only if the current one is not tiny.
            soft_boundary = etype == "subsection" and tokens_with(current) >= cfg.min_size
            if hard_boundary or soft_boundary:
                close(by_size=False)
        if etype == "table_header":
            if any(c.element["text"] == it.element["text"] for c in current):
                continue  # the continued table already carries these column names
            table_header = it
        elif etype in HEADING_TYPES:
            table_header = None
        if current and tokens_with([*current, it]) > cfg.size:
            close(by_size=True)
            if etype == "table_row" and table_header is not None and table_header not in current:
                current.insert(0, table_header)  # a continued table keeps its column names
            while current and tokens_with([*current, it]) > cfg.size:
                current.pop(0)  # carried overlap must not push the new chunk over the limit
        if tokens_with([it]) > cfg.size:
            close(by_size=False)
            groups.extend(_split_oversized(it, cfg, cfg_hash, pages_by_no, figures))
            started_by_size.extend([False] * (len(groups) - len(started_by_size)))
            continue
        current.append(it)
    close(by_size=False)

    if not cfg.overlap:
        _rebalance_tails(groups, started_by_size, tokens_with, cfg)
    return [
        g if isinstance(g, Chunk) else _make_chunk(g, cfg, cfg_hash, pages_by_no, figures)
        for g in groups
    ]


def _has_body(items: list[_Item]) -> bool:
    """Does a group contain anything besides headings?"""
    return any(it.element["type"] not in HEADING_TYPES for it in items)


def _rebalance_tails(groups, started_by_size, tokens_with, cfg: ChunkConfig) -> None:
    """A unit split by size can leave a tiny last piece ("Done with a pass? Tap ...").
    Move elements from the end of the previous piece until the two are balanced."""
    for i in range(1, len(groups)):
        prev, tail = groups[i - 1], groups[i]
        if not started_by_size[i] or isinstance(prev, Chunk) or isinstance(tail, Chunk):
            continue
        if tokens_with(tail) >= cfg.min_size:
            continue
        while len(prev) > 1 and prev[-1].element["type"] not in HEADING_TYPES:
            moved = prev[-1]
            at = 1 if tail and tail[0].element["type"] == "table_header" else 0
            candidate = tail[:at] + [moved] + tail[at:]
            if tokens_with(candidate) > cfg.size or tokens_with(prev[:-1]) < tokens_with(candidate):
                break
            prev.pop()
            tail[:] = candidate
        while len(prev) > 1 and prev[-1].element["text"].rstrip().endswith(":"):
            tail.insert(0, prev.pop())  # keep the intro line with what it introduces


def _split_oversized(
    it: _Item, cfg: ChunkConfig, cfg_hash: str, pages_by_no: dict, figures: dict
) -> list[Chunk]:
    """Safety net for an element longer than `size`.

    Only happens at size=200: three paragraphs of 184-188 tokens (pp. 151, 161) plus
    the header exceed it. They are split at sentence boundaries into `partial` chunks.
    """
    header_tokens = (
        count_tokens(_header(it.element["section_path"], [])) if cfg.header_prefix else 0
    )
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=cfg.size - header_tokens - 4,
        chunk_overlap=cfg.overlap,
        length_function=count_tokens,
        separators=[". ", "; ", ", ", " ", ""],
        keep_separator="end",
    )
    return [
        _make_chunk([it], cfg, cfg_hash, pages_by_no, figures, text=piece, partial=True)
        for piece in splitter.split_text(it.element["text"])
    ]


# --------------------------------------------------------------------------- naive baseline


def chunk_naive(
    pages: list[dict], figures: dict[tuple[int, int], list[str]], cfg: ChunkConfig, cfg_hash: str
) -> list[Chunk]:
    """Baseline: recursive splitter over the concatenated text, mapped back to elements."""
    pages_by_no = {p["page"]: p for p in pages}
    items = _items(pages, cfg.figure_mode)
    full, spans = "", []  # spans[i] = (start, end) offsets of items[i] in `full`
    for i, it in enumerate(items):
        if i:
            full += "\n\n" if it.page != items[i - 1].page else "\n"
        spans.append((len(full), len(full) + len(it.element["text"])))
        full += it.element["text"]
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=cfg.size, chunk_overlap=cfg.overlap, length_function=count_tokens
    )
    # Offsets are located manually: LangChain's add_start_index subtracts chunk_overlap
    # (here in tokens) from a character offset, which misplaces chunks.
    chunks, search_from = [], 0
    for piece in splitter.split_text(full):
        start = full.find(piece, search_from)
        if start < 0:
            raise ValueError(f"chunk text not found in source: {piece[:60]!r}")
        end, search_from = start + len(piece), start + 1
        covered = [items[i] for i, (s, e) in enumerate(spans) if s < end and e > start]
        chunks.append(_make_chunk(covered, cfg, cfg_hash, pages_by_no, figures, text=piece))
    return chunks


# --------------------------------------------------------------------------- driver

GRID = [
    ChunkConfig(size=size, overlap=overlap, header_prefix=header)
    for size in (200, 350, 500)
    for overlap in (0, round(size * 0.15))
    for header in (True, False)
] + [
    ChunkConfig(figure_mode="none"),
    ChunkConfig(strategy="naive", size=350, overlap=50, header_prefix=False),
]


def build_chunks(cfg: ChunkConfig) -> tuple[str, list[Chunk]]:
    """Chunk the parsed pages with one config; returns (config hash, chunks)."""
    pages, figures, parser_version = load_inputs()
    cfg_hash = cfg.config_hash(parser_version)
    fn = chunk_structure if cfg.strategy == "structure" else chunk_naive
    return cfg_hash, fn(pages, figures, cfg, cfg_hash)


def stats(chunks: list[Chunk]) -> dict:
    """Size and coverage statistics of a chunk set (written next to the chunks)."""
    toks = sorted(c.embed_token_count for c in chunks)
    per_page: dict[int, int] = {}
    for c in chunks:
        for pg in c.pages:
            per_page[pg] = per_page.get(pg, 0) + 1
    return {
        "chunks": len(chunks),
        "embed_tokens_total": sum(toks),
        "embed_tokens_p10": toks[len(toks) // 10],
        "embed_tokens_p50": toks[len(toks) // 2],
        "embed_tokens_p90": toks[9 * len(toks) // 10],
        "embed_tokens_max": toks[-1],
        "multi_page_chunks": sum(1 for c in chunks if len(c.pages) > 1),
        "cross_chapter_chunks": sum(
            1 for c in chunks if len({tuple(p) for p in c.element_refs}) and _crosses(c)
        ),
        "chunks_with_figures": sum(1 for c in chunks if c.has_figure),
        "max_chunks_per_page": max(per_page.values()),
    }


def _crosses(chunk: Chunk) -> bool:
    """Does a chunk contain elements from more than one chapter?"""
    pages = json_pages()
    chapters = {pages[pg]["chapter_number"] for pg, _ in chunk.element_refs}
    return len(chapters) > 1


@lru_cache(maxsize=1)
def json_pages() -> dict[int, dict]:
    """Parsed pages by page number (cached)."""
    with PAGES.open(encoding="utf-8") as f:
        return {p["page"]: p for p in map(json.loads, f)}


def write_chunks(cfg: ChunkConfig, out_dir: Path = OUT_DIR) -> dict:
    """Build one config and write <hash>.jsonl plus <hash>.config.json."""
    cfg_hash, chunks = build_chunks(cfg)
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / f"{cfg_hash}.jsonl").open("w", encoding="utf-8", newline="\n") as f:
        for c in chunks:
            f.write(json.dumps(asdict(c), ensure_ascii=False) + "\n")
    summary = {"chunk_config_hash": cfg_hash, "config": asdict(cfg), **stats(chunks)}
    (out_dir / f"{cfg_hash}.config.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return summary


def main() -> None:
    """CLI: build the default config, one custom config, or the whole grid."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--grid", action="store_true", help="build every config of the Step 3 grid")
    ap.add_argument("--strategy", choices=["structure", "naive"], default="structure")
    ap.add_argument("--size", type=int, default=350)
    ap.add_argument("--overlap", type=int, default=0)
    ap.add_argument("--no-header", action="store_true")
    ap.add_argument("--figure-mode", choices=["labels", "none"], default="labels")
    args = ap.parse_args()
    configs = (
        GRID
        if args.grid
        else [
            ChunkConfig(
                strategy=args.strategy,
                size=args.size,
                overlap=args.overlap,
                header_prefix=not args.no_header,
                figure_mode=args.figure_mode,
            )
        ]
    )
    rows = [write_chunks(cfg) for cfg in configs]
    if args.grid:
        (OUT_DIR / "summary.json").write_text(
            json.dumps(rows, indent=2) + "\n", encoding="utf-8", newline="\n"
        )
    for r in rows:
        c = r["config"]
        print(
            f"{r['chunk_config_hash']} {c['strategy']:<9} size={c['size']:<3} ov={c['overlap']:<3} "
            f"hdr={c['header_prefix']!s:<5} fig={c['figure_mode']:<6} chunks={r['chunks']:<4} "
            f"p50={r['embed_tokens_p50']:<3} max={r['embed_tokens_max']:<3} "
            f"multi_page={r['multi_page_chunks']:<3} cross_chapter={r['cross_chapter_chunks']}"
        )


if __name__ == "__main__":
    main()
