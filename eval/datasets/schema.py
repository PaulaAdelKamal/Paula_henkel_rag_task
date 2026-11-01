"""Schema of the evaluation datasets (golden and synthetic)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Category = Literal[
    "single_fact",  # one fact from one place
    "multi_step",  # a how-to with several steps
    "multi_page",  # the answer needs two or more pages/chunks
    "figure",  # the answer is in a diagram's callout labels
    "cross_reference",  # the answer is where a "See X on page N" points
    "multi_turn",  # a follow-up that needs the conversation history
    "model_near_miss",  # depends on the iPhone model (4/4s/5/5c/5s)
    "unanswerable",  # not in the guide: must be refused
    "adversarial",  # prompt injection, false premises, out-of-scope requests
]
Difficulty = Literal["easy", "medium", "hard"]
ReviewStatus = Literal["draft", "accepted", "edited", "rejected"]


class Evidence(BaseModel):
    page: int  # printed page number (= PDF page)
    quote: str  # verbatim text from that page (checked by build.py)


class Turn(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class DraftItem(BaseModel):
    """What a person (or an LLM) writes; gold pages and sections are derived, not typed."""

    id: str
    question: str
    reference_answer: str
    answerable: bool
    category: Category
    difficulty: Difficulty
    evidence: list[Evidence] = Field(default_factory=list)
    history: list[Turn] = Field(default_factory=list)  # prior turns for multi_turn items
    notes: str = ""
    review_status: ReviewStatus = "draft"
    reviewer_notes: str = ""


class GoldenItem(DraftItem):
    """A validated item: gold pages and sections derived from where the evidence was found."""

    gold_pages: list[int]
    gold_sections: list[str]
    source: str = "drafted by Claude (Opus 5.5) from data/parsed/pages.jsonl"
