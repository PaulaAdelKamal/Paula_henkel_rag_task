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
