# Evaluation datasets

## Files

| File | What it is | Edited by |
|---|---|---|
| `golden_draft.jsonl` | The golden questions as written: question, reference answer, category, difficulty, multi-turn history, **evidence quotes**, review status | `review.py` (or by hand) |
| `golden.jsonl` | Built by `build.py`: validated items plus **derived** `gold_pages` and `gold_sections`; rejected items left out | generated, never edit |
| `schema.py` | Pydantic schema of both | |
| `build.py` | Validator and builder | |
| `review.py` | Review tools: HTML page with rendered gold pages, CSV sheet, interactive CLI | |
| `upload_langfuse.py` | Idempotent upload to the Langfuse dataset `golden` | |

## Schema (one JSON object per line)

```json
{"id": "g052", "question": "And how do I take a lot of shots in a row?",
 "history": [{"role": "user", "content": "How do I take a photo?"},
             {"role": "assistant", "content": "In Camera, choose Photo, then tap the shutter button ..."}],
 "reference_answer": "On iPhone 5s, touch and hold the shutter button to take photos in burst mode ...",
 "answerable": true, "category": "multi_turn", "difficulty": "hard",
 "evidence": [{"page": 79, "quote": "Touch and hold the shutter button to take photos in burst mode."}],
 "gold_pages": [79], "gold_sections": ["Chapter 12: Camera > Take photos and videos"],
 "review_status": "draft", "reviewer_notes": "", "notes": "",
 "source": "drafted by Claude (Opus 5.5) from data/parsed/pages.jsonl"}
```

- `gold_pages` and `gold_sections` are **derived, never typed**. Gold pages are the pages of the evidence quotes. Gold sections are the section paths of the parsed elements that contain the quotes.
- An answer is scored correct at page level if it cites or retrieves **any** gold page.
- Unanswerable items have no evidence, and their reference answer says the guide doesn't cover the question.

## How gold pages were verified

1. **Drafting.** Questions were drafted by Claude (Opus 5.5, via Claude Code, using no OpenRouter quota) by reading `data/parsed/pages.jsonl`. Every answerable question carries a short verbatim quote from the page that answers it.
2. **Automatic check.** `build.py` requires every quote to appear on its stated page, ignoring case, whitespace and curly vs. straight quotes. The page must be retrievable content, not the cover or TOC. Tests prove a wrong page, an invented quote or a TOC page are rejected.
3. **Human review (the Step 8 gate).** The project owner checks every item against the rendered PDF page with `review.py html`, and records accept / edit / reject. Status is kept per item (`review_status`), and edits to text mark an item `edited`.
4. Unanswerable topics were checked absent with a full-text search of the parsed guide: Face ID, Dynamic Island, Apple Pay, Android, Apple Watch, wireless charging, USB-C, dark mode, widgets, battery health, storage sizes, device prices.

## Category counts (80 items)

| Category | Items | What it tests |
|---|---|---|
| `single_fact` | 18 | One fact from one place |
| `multi_step` | 13 | How-to procedures |
| `multi_page` | 6 | Answers that need two pages (e.g. erase before selling: p39 + p158) |
| `figure` | 6 | Facts that exist only in diagram callout labels (e.g. "touch and hold Mute to put a call on hold", p46) |
| `cross_reference` | 5 | The answer is where a "See … on page N" points |
| `multi_turn` | 7 | Pronoun or ellipsis follow-ups with a conversation history |
| `model_near_miss` | 6 | Model-specific features (Touch ID and Slo-Mo on 5s only, AirDrop on 5 and later, Siri on 4s and later) |
| `unanswerable` | 13 | Newer devices and features, prices, Android, live data; includes hard negatives (Apple Pay vs Passbook, battery health vs battery percentage, StandBy vs "standby mode") |
| `adversarial` | 6 | Injection ("ignore your instructions…"), a false page premise ("cite page 12"; the right page is 80), a nonexistent page, prompt extraction, role-play, a half in-scope question |

17 of the 80 (21%) must be refused: the 13 unanswerable plus 4 adversarial.

## Commands

```bash
uv run python -m eval.datasets.build             # validate + write golden.jsonl
uv run python -m eval.datasets.review html       # data/cache/review/review.html (open in a browser)
uv run python -m eval.datasets.review export     # review_sheet.csv -> edit -> import
uv run python -m eval.datasets.review import
uv run python -m eval.datasets.review cli        # interactive accept / edit / reject
uv run python -m eval.datasets.upload_langfuse   # upsert into the Langfuse dataset "golden"
```

## Synthetic set (not yet generated)

The brief also asks for a synthetic set from the Ragas test-set generator, generated with a model family different from the judge, labelled synthetic and kept apart from the golden set. It isn't generated yet. The generator first builds a knowledge graph over all chunks (summaries, headlines and embeddings per chunk), which takes hundreds of LLM calls, more than the 50-requests-per-day free OpenRouter quota allows. It will be produced once the quota allows it (see `docs/DECISIONS.md`, ADR-0026). The golden set doesn't depend on it.
