"""Turn chunk metadata into the citations every response must show.

Citations are derived from the payloads of the chunks the model said it used
(after validation), never from model-written text, so page numbers are always
real. Refusals list the closest sections that were searched, explicitly
labelled as not being sources of an answer.
"""

from __future__ import annotations

from dataclasses import dataclass

LEGAL = "Legal notices (back page)"


@dataclass(frozen=True)
class Citation:
    """One user-facing citation: the pages a chunk spans and its section path."""

    pages: tuple[int, ...]
    section: str  # "Chapter 3: Basics > Use apps > Multitasking"

    @property
    def page_label(self) -> str:
        """ "Page 22" for one page, "Pages 12–13" for a range."""
        if len(self.pages) == 1:
            return f"Page {self.pages[0]}"
        return f"Pages {self.pages[0]}–{self.pages[-1]}"

    def render(self) -> str:
        """The line shown under Sources: "Page 22 · Chapter 3: Basics > …"."""
        return f"{self.page_label} · {self.section}"


def citation_for(chunk: dict) -> Citation:
    """Build the citation of one chunk from its metadata (never from model output)."""
    path = chunk.get("section_path") or []
    section = " > ".join(path) if path else LEGAL
    return Citation(pages=tuple(chunk.get("pages") or [chunk["page"]]), section=section)


def unique_citations(chunks: list[dict], by_page: bool = True) -> list[Citation]:
    """One line per (pages, section). Sources are ordered by page; with by_page=False the
    input order (retrieval rank) is kept, which is what "closest sections" needs."""
    seen: dict[tuple, Citation] = {}
    for c in chunks:
        cit = citation_for(c)
        seen.setdefault((cit.pages, cit.section), cit)
    out = list(seen.values())
    return sorted(out, key=lambda c: (c.pages, c.section)) if by_page else out


def render_answer(answer: str, used: list[dict]) -> str:
    """Answer text followed by a **Sources** block listing the chunks the model used."""
    lines = [answer.strip(), "", "**Sources**"]
    lines += [f"- {c.render()}" for c in unique_citations(used)]
    return "\n".join(lines)


def render_refusal(message: str, searched: list[dict], limit: int = 3) -> str:
    """Refusal text plus the closest sections searched, labelled as not being sources."""
    lines = [message]
    closest = unique_citations(searched, by_page=False)[:limit]
    if closest:
        lines += ["", "**Closest sections searched (not sources of an answer)**"]
        lines += [f"- {c.render()}" for c in closest]
    return "\n".join(lines)
