"""Chunk distribution plots for docs/CHUNKING.md.

Usage: uv run python -m scripts.plot_chunks   (after `python -m ingestion.chunking --grid`)

Writes docs/img/chunk_tokens.png (small multiples, one per config) and
docs/img/chunks_per_page.png (default config). Single series per panel, so one
hue (reference palette slot 1) and no legend; text uses text tokens.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

CHUNKS = Path("data/chunks")
OUT = Path("docs/img")
SURFACE = "#fcfcfb"
TEXT = "#0b0b0b"
TEXT_2 = "#52514e"
GRID = "#e4e3df"
SERIES = "#2a78d6"

PANELS = [  # (label, strategy, size, overlap, header)
    ("Structure-aware, size 200", "structure", 200, 0, True),
    ("Structure-aware, size 350 (default)", "structure", 350, 0, True),
    ("Structure-aware, size 500", "structure", 500, 0, True),
    ("Naive recursive splitter, size 350", "naive", 350, 50, False),
]


def load(strategy: str, size: int, overlap: int, header: bool) -> list[dict]:
    for cfg_file in CHUNKS.glob("*.config.json"):
        c = json.loads(cfg_file.read_text(encoding="utf-8"))["config"]
        if (c["strategy"], c["size"], c["overlap"], c["header_prefix"], c["figure_mode"]) == (
            strategy,
            size,
            overlap,
            header,
            "labels",
        ):
            path = cfg_file.with_name(cfg_file.name.replace(".config.json", ".jsonl"))
            with path.open(encoding="utf-8") as f:
                return [json.loads(line) for line in f]
    raise FileNotFoundError(f"run ingestion.chunking --grid first ({strategy}, {size})")


def style(ax) -> None:
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=TEXT_2, labelsize=8)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def plot_tokens() -> None:
    fig, axes = plt.subplots(len(PANELS), 1, figsize=(7, 8), sharex=True, facecolor=SURFACE)
    bins = list(range(0, 520, 20))
    for ax, (label, strategy, size, overlap, header) in zip(axes, PANELS, strict=True):
        chunks = load(strategy, size, overlap, header)
        tokens = [c["embed_token_count"] for c in chunks]
        style(ax)
        ax.hist(tokens, bins=bins, color=SERIES, edgecolor=SURFACE, linewidth=1.5)
        top = ax.get_ylim()[1] * 1.25  # headroom so the limit label clears the bars
        ax.set_ylim(0, top)
        ax.axvline(size, color=TEXT_2, linewidth=1, linestyle=(0, (3, 3)))
        ax.text(size, top * 0.88, f" limit {size}", color=TEXT_2, fontsize=8)
        median = sorted(tokens)[len(tokens) // 2]
        ax.set_title(
            f"{label}: {len(chunks)} chunks, median {median} tokens",
            loc="left",
            fontsize=9,
            color=TEXT,
        )
        ax.set_ylabel("chunks", fontsize=8, color=TEXT_2)
    axes[-1].set_xlabel(
        "Embedded tokens per chunk (BERT WordPiece, header included)", fontsize=8, color=TEXT_2
    )
    fig.tight_layout()
    fig.savefig(OUT / "chunk_tokens.png", dpi=150, facecolor=SURFACE)
    plt.close(fig)


def plot_per_page() -> None:
    chunks = load("structure", 350, 0, True)
    per_page = Counter(pg for c in chunks for pg in c["pages"])
    counts = Counter(per_page.values())
    fig, ax = plt.subplots(figsize=(7, 2.6), facecolor=SURFACE)
    style(ax)
    xs = sorted(counts)
    ax.bar(xs, [counts[x] for x in xs], color=SERIES, edgecolor=SURFACE, linewidth=1.5, width=0.8)
    for x in xs:
        ax.text(x, counts[x], str(counts[x]), ha="center", va="bottom", fontsize=8, color=TEXT_2)
    ax.set_ylim(0, max(counts.values()) * 1.2)
    ax.set_xticks(xs)
    ax.set_xlabel("Chunks touching a page (default config)", fontsize=8, color=TEXT_2)
    ax.set_ylabel("pages", fontsize=8, color=TEXT_2)
    ax.set_title(
        f"Chunks per page: {len(per_page)} retrievable pages", loc="left", fontsize=9, color=TEXT
    )
    fig.tight_layout()
    fig.savefig(OUT / "chunks_per_page.png", dpi=150, facecolor=SURFACE)
    plt.close(fig)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    plot_tokens()
    plot_per_page()
    print(f"wrote {OUT / 'chunk_tokens.png'} and {OUT / 'chunks_per_page.png'}")


if __name__ == "__main__":
    main()
