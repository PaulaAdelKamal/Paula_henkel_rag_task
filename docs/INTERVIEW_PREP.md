# Interview prep

One section per step: likely questions and crisp answers, including trade-offs and what was rejected.

---

## Step 0: what I must be able to explain

**1. How do you guarantee that no API key ever reaches the repo or the image?**
Four layers, starting before the first line of code:
- `.env` is listed in `.gitignore` *and* `.dockerignore` in the very first commit.
- Keys are `SecretStr` in config, so they are masked in logs and reprs, and config errors name the variable, never its value.
- Two secret scanners run on every commit (detect-secrets with a baseline, gitleaks on staged changes). gitleaks also scans the full history on push and in CI.
- `.env.example` holds only placeholders.

If a key ever leaks, the order is: rotate the key first, then clean history. Once a key is pushed it must be treated as compromised, even after a history rewrite.

**2. Why two secret scanners?**
They detect differently. detect-secrets uses entropy plus plugins, with a reviewed baseline. gitleaks uses a large rule set of provider-specific regexes. The cost is tiny, and a miss is expensive. gitleaks in CI also covers anyone who commits without the hooks installed.

**3. Why uv and dependency groups?**
- `uv.lock` pins every transitive package with hashes, so the build is reproducible.
- Groups keep the runtime image slim: only `[project.dependencies]` ships. `dev`, `ingest` and later `eval` (ragas, PyMuPDF, pytest) never enter the container.
- That also keeps PyMuPDF's AGPL licence out of the distributed image.

**4. Why Streamlit when the brief prefers Chainlit?**
Chainlit 2.12, the latest, declares `Requires-Python <3.14`. Our interpreter is 3.14, and on this machine no other interpreter can run (app control). Running Chainlit outside its declared support range would be a hidden risk.

What changes with Streamlit:
- The script re-runs on every interaction, so session state is explicit (`st.session_state`, plus a LangGraph checkpointer keyed by a per-session `thread_id`).
- Feedback uses `st.feedback`.

The mandatory requirements (citations, refusals, multi-turn, single container) don't depend on the UI framework.

**5. Why Qdrant Cloud over Pinecone?**
The benchmark needs one index per embedding model, and dimensions differ (384 to 4096). Qdrant's free cluster holds many collections with different dimensions, plus payload indexes and sparse vectors for hybrid search. Pinecone's free tier limits the number of indexes, and each index has one dimension.

The risk to manage is the free-tier inactivity policy: suspended after 1 week idle, deleted after 4 weeks. Mitigation: a keep-alive / verify script, and a re-check before submission and before the interview.

**6. Why route every model through OpenRouter?**
- One key, one bill, one OpenAI-compatible API for chat, embeddings, vision and the judge.
- The listing endpoints let us discover and record exact model slugs instead of typing them from memory.
- `GET /key` validates a key at zero cost.

Trade-off: a single point of failure, and per-model behaviour (e.g. embedding prefixes) is our responsibility.

**7. Why `LANGFUSE_BASE_URL` and not `LANGFUSE_HOST` as in the brief?**
The current Langfuse Python SDK (v5) reads `LANGFUSE_BASE_URL` and marks `LANGFUSE_HOST` as deprecated. Following the SDK means its auto-configuration works without glue code. Tracing is optional: missing keys disable it, they don't crash the app.

**8. What does `scripts/check_connections.py` actually verify, and why doesn't it spend money?**
- OpenRouter: `GET /key` (key validity, remaining limit), and that the configured chat and embedding slugs exist in the live listings.
- Qdrant: `get_collections()`.
- Langfuse: `auth_check()`, reported as SKIP if no keys are set.

None of these calls is billable. Every failure prints as one line, with secret values redacted.

---

## Step 1: what I must be able to explain

**1. How did you choose the PDF extraction library?**
By measurement, not reputation.
- I wrote anchors by hand from six rendered hard pages (a diagram, two tables, a list of links, a two-column legal page, a heading-dense page), i.e. the strings a human reads in order.
- I scored six extractor configurations on them (`scripts/compare_extractors.py`).
- PyMuPDF in native content order got 36/36 and ran in 0.5 s. pdfplumber garbled the doubly drawn diagram labels and interleaved the legal page's columns. pypdf tied on order but is 8× slower and gives no font information.
- Font information was the deciding factor: every structural rule depends on it.

**2. PyMuPDF is AGPL. Is that a problem?**
Not in this design.
- It's only in the offline `ingest` dependency group and never in the Docker image or the running service, so the distributed chatbot doesn't contain AGPL code.
- What ships is data (`pages.jsonl`) plus the vectors in Qdrant.
- If the licence were unacceptable, pypdf (BSD) produced the same reading order on our hard pages. I'd have to rebuild style detection with a visitor, which is extra work but not a redesign.

**3. Why not just extract text page by page and split it?**
Because citations and retrieval quality depend on structure.
- Each element carries its chapter > section > subsection path, which becomes the contextual header in Step 3 and the "Section" in citations.
- Without that, a chunk like "Close an app. Drag the app up…" doesn't say it's in Basics > Multitasking.
- The running footer would also pollute chunks if left in text, so it's stripped and harvested as metadata instead.

**4. How do you know the section map is right?**
Three independent sources agree, and tests pin it:
- the printed TOC (36 chapters, 212 sections);
- font-detected headings: every TOC section is a Medium 14 pt blue heading on its TOC page;
- the running footer: it agrees with the TOC chapter on all 118 footer pages.

Printed page = PDF page is asserted on every numbered page, because a wrong page number in a citation fails the grader's check.

**5. What did you do with the table of contents?**
- It's parsed into `toc.json` as ground truth, but excluded from retrieval.
- A TOC line such as "45 Chapter 6: Mail" is lexically similar to every Mail question. It would win retrieval slots and cite page 4 instead of the real page.
- The legal page stays retrievable but tagged, because it is part of the document.

**6. The figures have no captions. What did you extract?**
The callout labels, which are in the text layer.
- They're printed twice (a white halo and a dark copy), so the halo is dropped by colour.
- Wrapped label lines are joined by line pitch (8.8 pt inside a label, ≥ 16 pt between labels) and emitted column by column as a `figure_labels` element.
- Result: 74 label groups on 63 pages, e.g. "Swipe left to use the level." on the Compass page, or the full iPhone 5s/5c anatomy on p. 8.
- Text *inside* the raster screenshots is a separate question for Step 2 (OCR / vision captions, kept only if evaluation shows they help).

**7. How did you validate the parser beyond unit tests?**
- A 15-page random visual spot check (fixed seed), comparing rendered pages to parsed elements.
- It found two real bugs: wrapped bullets split in two, and bullets split by an inline icon. Both were fixed and are now covered by tests.
- It also found limitations I kept on purpose and documented: column-major label order, inline icon names lost, screenshot text not extracted.
- A refactor was checked by diffing the output: byte-identical.

**8. What are the parser's limits?**
- It's tuned to this guide's typography (fonts, sizes, the blue #007ac2). That's the right trade-off for a single-document chatbot and is enforced by tests, but it wouldn't generalise to other PDFs without new rules.
- Inline icons lose their meaning ("tap ✈" becomes "tap.").
- One cross-reference label over-captures (164 of 165 resolve).

---

## Step 2 (skipped): what I must be able to explain

**1. The brief planned vision captions and OCR for figures. Why didn't you do it?**
I measured first.
- The 434 image placements are really 122 figures; many drawings are assembled from slices.
- They're about 140 px wide, so OCR would be mostly garbage and captions coarse.
- Every figure sits under the instruction it illustrates, so that instruction is effectively the caption, and it's already indexed.
- The text that matters inside figures, the callout labels, is in the PDF's text layer and was extracted in Step 1.

So captions would mostly duplicate indexed text, and they carry a real risk (next question).

**2. What's the risk of captioning?**
Grounding. A caption is a model's interpretation, not the document. If the vision model invents a UI detail, that detail gets indexed, and the bot can cite page N for something page N doesn't say. That breaks the one hard requirement: answer strictly from the document. Captions also add text that dilutes chunk embeddings.

**3. So figures are ignored?**
No. Figure regions are recorded with their anchor instruction and callout labels, and chunks carry `has_figure` / `figure_ids`. The UI can show the source page, and evaluation can slice by figure-dependent questions. If those questions fail at retrieval, I caption only the affected figures and measure again. Captioning becomes a switch you turn on with evidence.

---

## Step 3: what I must be able to explain

**1. What chunking strategy did you choose, and why for this document?**
Structure-aware packing of whole paragraphs, list items and table rows, with chapter and section boundaries as hard limits.
- The guide is made of short, titled how-to units (median subsection 173 tokens), and answers are usually one task paragraph on one page.
- Following that structure keeps each chunk on one topic and keeps page citations precise.
- A fixed-size splitter cuts tasks in half and even glues the end of one chapter to the start of the next. The baseline did that 7 times.

**2. Why 350 tokens and no overlap?**
- **350:** big enough that most subsections stay whole (the median chunk is 188 tokens; only long sections get split), and small enough to fit the 512-token embedding models with room for prefixes.
- **No overlap:** chunks end at paragraph or heading boundaries, so overlap would mostly duplicate text. I measured that 15% overlap changes only 3 of 336 chunks.

Both are starting points. Every alternative is built and will be compared on page-level retrieval metrics in Step 10.

**3. Why did you change the brief's 300/500/800 grid?**
BGE, E5 and GTE truncate input at 512 tokens. An 800-token chunk would be silently cut for half the candidates, so the embedding benchmark would compare different texts per model. The grid is 200/350/500, and every chunk is tested to fit.

**4. How do you count tokens when every model has its own tokenizer?**
- I count in BERT WordPiece, the tokenizer of the strictest models, using a vocabulary file committed to the repo, so chunking is deterministic and offline.
- Long-context models tokenize English to similar or fewer tokens, so the limit is conservative for them.
- In Step 4 I check the real per-model counts against the `usage` the embeddings API returns.

**5. What is the contextual header and why embed it?**
- The path "iPhone User Guide > Chapter 3: Basics > Use apps > Multitasking" is prepended to the chunk before embedding.
- A task paragraph like "Drag the app up from the multitasking display" never names its topic; the header does, which helps both semantic and keyword matching.
- It costs about 8% more tokens. It isn't shown to the user (`text` vs `embed_text`), and "header off" is in the grid to measure its effect.

**6. Walk me through the metadata.**
- Pages: `page`, `pages` and the 0-based index; every touched page is cited.
- Chapter and section path: the citation's "Section", and payload filters.
- `content_type`, `lead_ins` (task titles), figure ids, cross-references.
- Separate display text and embedded text.
- A deterministic UUIDv5 id, so re-ingestion is idempotent.
- `chunk_config_hash`, which covers config + chunker + parser versions, so a stale collection can't be reused by accident.
- `element_refs`, tracing each chunk back to parsed elements. The tests use it to prove nothing is lost.

**7. What edge cases did you handle?**
- An intro line ending in ":" never ends a chunk; it stays with its list.
- A table continuing into a new chunk repeats its column names once.
- Tiny leftover pieces are rebalanced (chunks under 64 tokens went from 37 to 26).
- Paragraphs longer than the limit (only at size 200) are split at sentences into `partial` chunks.
- Two bugs were caught by tests and fixed:
  - an intro line was silently dropped at section boundaries (now tested: every element lands in a chunk);
  - LangChain's `add_start_index` misplaced chunks because it subtracts a token-unit overlap from character offsets.

**8. What would make you change the chunking?**
Step 10 evidence on page-level hit@k and MRR across the grid, with confidence intervals. If size 200 or 500, overlap, or "header off" win beyond noise, the default changes and an ADR records it. If the differences are within noise, the simpler or cheaper option stays.
