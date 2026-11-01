"""The golden set: builder/validator, review tooling and Langfuse payloads (offline)."""

import json

import pytest

from eval.datasets.build import build
from eval.datasets.review import apply_decision, export_csv, import_csv, load
from eval.datasets.schema import DraftItem
from eval.datasets.upload_langfuse import item_payload

GOOD = {
    "id": "t001",
    "question": "How do I take a screenshot?",
    "reference_answer": "Press and release Sleep/Wake and Home together.",
    "answerable": True,
    "category": "single_fact",
    "difficulty": "easy",
    "evidence": [
        {"page": 80, "quote": "Simultaneously press and release the Sleep/Wake and Home buttons."}
    ],
}


def write_draft(tmp_path, *items):
    path = tmp_path / "draft.jsonl"
    path.write_text("\n".join(json.dumps(i) for i in items) + "\n", encoding="utf-8")
    return path


def errors_for(tmp_path, **changes):
    _, problems = build(write_draft(tmp_path, GOOD | changes))
    return problems


# --- the committed golden set ---------------------------------------------------------


def test_committed_golden_draft_is_valid_and_balanced():
    items, problems = build()
    assert problems == []
    assert len(items) >= 60
    categories = {it.category for it in items}
    assert categories == {
        "single_fact",
        "multi_step",
        "multi_page",
        "figure",
        "cross_reference",
        "multi_turn",
        "model_near_miss",
        "unanswerable",
        "adversarial",
    }
    unanswerable = sum(not it.answerable for it in items) / len(items)
    assert 0.15 <= unanswerable <= 0.25  # brief: about 15-20% unanswerable
    for it in items:
        if it.answerable:
            assert it.gold_pages and it.gold_sections
            assert all(8 <= p <= 162 for p in it.gold_pages)
        if it.category == "multi_turn":
            assert it.history and it.history[0].role == "user"


def test_gold_pages_and_sections_are_derived_from_the_evidence(tmp_path):
    (item,), problems = build(write_draft(tmp_path, GOOD))
    assert problems == []
    assert item.gold_pages == [80]
    assert item.gold_sections == ["Chapter 12: Camera > Take photos and videos"]


def test_quote_matching_ignores_case_whitespace_and_curly_quotes(tmp_path):
    quote = "iPhone locks automatically if you don't   touch the screen for a minute or so."
    assert errors_for(tmp_path, evidence=[{"page": 10, "quote": quote.upper()}]) == []


# --- the validator rejects bad items ------------------------------------------------------


def test_quote_not_on_its_page_is_rejected(tmp_path):
    problems = errors_for(tmp_path, evidence=[{"page": 79, "quote": GOOD["evidence"][0]["quote"]}])
    assert any("quote not found on p79" in p for p in problems)


def test_invented_quote_is_rejected(tmp_path):
    problems = errors_for(
        tmp_path, evidence=[{"page": 80, "quote": "Say 'cheese' to take a photo."}]
    )
    assert any("quote not found" in p for p in problems)


def test_toc_pages_are_not_valid_evidence(tmp_path):
    problems = errors_for(tmp_path, evidence=[{"page": 3, "quote": "Siri"}])
    assert any("p3 is not retrievable" in p for p in problems)


def test_answerable_needs_evidence_and_unanswerable_must_not_have_it(tmp_path):
    assert any("without evidence" in p for p in errors_for(tmp_path, evidence=[]))
    problems = errors_for(tmp_path, answerable=False, category="unanswerable")
    assert any("must not have evidence" in p for p in problems)


def test_multi_turn_history_must_alternate_from_the_user(tmp_path):
    bad = [{"role": "assistant", "content": "hi"}, {"role": "user", "content": "q"}]
    problems = errors_for(tmp_path, category="multi_turn", history=bad)
    assert any("multi_turn history" in p for p in problems)


def test_duplicate_ids_are_rejected(tmp_path):
    _, problems = build(write_draft(tmp_path, GOOD, GOOD))
    assert any("duplicate id" in p for p in problems)


def test_rejected_items_are_left_out(tmp_path):
    items, problems = build(write_draft(tmp_path, GOOD | {"review_status": "rejected"}))
    assert problems == [] and items == []


# --- review tooling --------------------------------------------------------------------------


def test_review_decisions():
    item = DraftItem.model_validate(GOOD)
    assert apply_decision(item, "accepted").review_status == "accepted"
    assert apply_decision(item, "rejected", notes="dup").reviewer_notes == "dup"
    edited = apply_decision(item, "accepted", question="How can I capture my screen?")
    assert edited.review_status == "edited" and edited.question == "How can I capture my screen?"
    same = apply_decision(item, "accepted", question=item.question)  # unchanged text stays accepted
    assert same.review_status == "accepted"
    with pytest.raises(ValueError):
        apply_decision(item, "maybe")


def test_csv_round_trip_applies_reviewer_edits(tmp_path):
    items = load()[:3]
    sheet = tmp_path / "sheet.csv"
    export_csv(items, sheet)
    text = sheet.read_text(encoding="utf-8-sig")
    lines = text.splitlines()
    header = lines[0].split(",")
    assert header[0] == "id" and "review_status" in header
    # reject the second item by editing its status cell
    rows = text.splitlines()
    rows[2] = rows[2].replace(",draft,", ",rejected,")
    sheet.write_text("\n".join(rows) + "\n", encoding="utf-8-sig")
    updated = import_csv(items, sheet)
    assert [u.review_status for u in updated] == ["draft", "rejected", "draft"]


# --- Langfuse payloads --------------------------------------------------------------------------


def test_langfuse_payload_shape():
    items, _ = build()
    multi_turn = next(it for it in items if it.category == "multi_turn")
    p = item_payload(multi_turn)
    assert p["id"] == f"golden-{multi_turn.id}" and p["status"] == "ACTIVE"
    assert p["input"]["history"] and p["input"]["question"] == multi_turn.question
    assert set(p["expected_output"]) == {
        "reference_answer",
        "answerable",
        "gold_pages",
        "gold_sections",
    }
    assert p["metadata"]["category"] == "multi_turn"
