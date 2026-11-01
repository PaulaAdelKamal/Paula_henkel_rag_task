# Decision log (lightweight ADRs)

Each entry: context, options, decision, consequences, evidence. Status is *accepted*, *provisional* (to be re-checked with experiment data) or *superseded*.

---

## ADR-0001: Python 3.14, managed with uv and dependency groups

**Status:** accepted (Step 0)

**Context.** The dev machine runs enterprise application control. Freshly downloaded interpreters (a uv-managed Python 3.12) and native binaries are blocked; only the already approved Python 3.14 interpreters can run. Python 3.14 has been stable since October 2025, so it is a mainstream choice today.

**Options.** (a) Python 3.12/3.13 in Docker only, 3.14 locally: two interpreters, so tests run on a version that doesn't ship. (b) Python 3.14 everywhere. (c) Ask IT to approve another interpreter: slow, and the clock is running.

**Decision.** (b) Python 3.14 locally, in CI and in the image. `uv` manages the environment. `uv.lock` pins exact versions with hashes. Dependencies are split into groups: runtime (`[project.dependencies]`, the only thing in the image), `dev` (lint, tests, hooks) and `ingest` (PDF parsing). Later steps add an `eval` group.

**Consequences.** A single interpreter version everywhere, so "works on my machine" equals "works in the container". Any dependency that caps `Requires-Python` below 3.14 is out; that is what triggered ADR-0002. uv ignores upper bounds on `requires-python` when resolving, so every new dependency's metadata must be checked by hand.

**Evidence.** `Requires-Python` metadata of the locked packages. The blocked 3.12 launch (`Access is denied`) is noted in the Step 0 summary.

---

## ADR-0002: Streamlit instead of Chainlit for the UI

**Status:** accepted (Step 0; the project owner decided)

**Context.** The brief prefers Chainlit, but doesn't require it. Chainlit 2.12.0, the latest, declares `Requires-Python: <3.14,>=3.10`, which conflicts with ADR-0001.

**Options.** (a) Run Chainlit on 3.14 anyway: unsupported, could break at runtime. (b) Downgrade Python, which ADR-0001 rules out. (c) Streamlit 1.66.1 (`Requires-Python >=3.11`).

**Decision.** (c) Streamlit.

**Consequences.**
- Streamlit re-runs the whole script on every interaction. Chat history therefore lives in `st.session_state`, and graph memory sits in a LangGraph checkpointer keyed by a per-session `thread_id` (a UUID stored in session state).
- Streaming uses `st.write_stream`. Thumbs feedback uses `st.feedback`, sent to Langfuse as a score.
- The page preview can render the source page as an image from the committed PDF.
- The server must bind `0.0.0.0:8000` (`--server.address 0.0.0.0 --server.port 8000`) and turn off usage stats (`--browser.gatherUsageStats false`).
- The interview question "why not Chainlit?" has a factual answer: it doesn't support the interpreter version.

**Evidence.** `chainlit-2.12.0.dist-info/METADATA`: `Requires-Python: <3.14.0,>=3.10`.

---

## ADR-0003: Local hooks for secret scanning and linting (two scanners)

**Status:** accepted (Step 0)

**Context.** pre-commit's usual remote hook repos can't install on this machine: `git submodule` fails because app control denies Git's own coreutils, and Go and `ruff.exe` downloads are blocked as well.

**Decision.**
- Every hook is `repo: local`.
- Python tools run as modules from the project venv (`detect-secrets`, `pre-commit-hooks`).
- Native tools run from pinned official Docker images: `zricethezav/gitleaks:v8.30.1` and `ghcr.io/astral-sh/ruff:0.17.3`.
- Two independent secret scanners: detect-secrets with a committed baseline, plus gitleaks on staged changes. gitleaks also scans the **full history** on `pre-push`.
- CI runs ruff and pytest natively, and the same pinned gitleaks image over the full history.
  `gitleaks-action` was tried first and dropped: it failed with exit code 1 on the first push of
  a new branch while the same commits scanned clean locally, and running one pinned image in both
  places makes the scans identical.

**Consequences.** Docker Desktop must be running to commit. That is fail-closed: if Docker is down, the commit is blocked rather than silently skipping the scan.

---

## ADR-0004: Qdrant Cloud (free tier) as the vector database

**Status:** provisional (Step 0; re-checked when collections are created in Step 4)

**Context.** The benchmark needs one index per embedding model, and the models have different dimensions. Retrieval also needs payload filtering, and hybrid dense + sparse search is optional.

**Options.** Pinecone free tier: limited number of indexes, and each index has a single fixed dimension. Qdrant Cloud free tier: one cluster, many collections, payload indexes, sparse vectors.

**Decision.** Qdrant Cloud, with one collection per (embedding model, chunk config), named `iphone_guide__{model}__{chunk_hash}`.

**Consequences.** The free cluster has 1 node, 1 GB RAM and 4 GB disk; Qdrant states that holds about 1M vectors at 768 dimensions. Our index is a few thousand chunks per model, far below that. **Inactivity policy:** an unused free cluster is suspended after 1 week and deleted after 4 weeks. Hence `ingestion/verify_index.py` (keep-alive) and a re-check right before submission and again before the interview.

**Evidence.** https://qdrant.tech/documentation/cloud/create-cluster/ (checked 2026-10-31).

---

## ADR-0005: OpenRouter as the single model provider

**Status:** accepted (Step 0)

**Decision.** Chat, embeddings, vision captioning and the Ragas judge all go through OpenRouter's OpenAI-compatible API (`https://openrouter.ai/api/v1`). That means one key and one billing dashboard. Model names are discovered from the listing endpoints (`/models`, `/embeddings/models`) rather than typed from memory. `GET /key` validates the key without spending credit.

**Consequences.** The open-weight embedding families from the brief are all available: BGE (`baai/bge-m3`, `bge-large/base-en-v1.5`), E5 (`intfloat/e5-large-v2`, `multilingual-e5-large`), GTE (`thenlper/gte-large`) and Qwen3-Embedding (`4b`, `8b`). So are newer ones (NVIDIA Nemotron embed, Perplexity pplx-embed). The shortlist is chosen in Step 4. One provider is also a single point of failure; acceptable for a demo.

---

## ADR-0006: Langfuse Cloud, SDK v5, `LANGFUSE_BASE_URL`

**Status:** accepted (Step 0)

**Context.** Self-hosting Langfuse needs Postgres, ClickHouse, Redis and blob storage, which conflicts with "single container + .env". The current SDK is v5 (5.1.0). It reads `LANGFUSE_BASE_URL`; `LANGFUSE_HOST` is marked deprecated in the SDK source.

**Decision.** Langfuse Cloud on the Hobby tier (50k units per month, 30-day data access). The env var is `LANGFUSE_BASE_URL`, which deliberately deviates from the brief's `LANGFUSE_HOST` to follow the current SDK. Tracing is optional: with no keys, the app runs without it.

---

## ADR-0007: Use the OS trust store for TLS (`truststore`)

**Status:** accepted (Step 0)

**Context.** The dev network does TLS inspection: a corporate root CA sits in the certificate chain, so Python's bundled `certifi` CA list rejects every HTTPS call (`CERTIFICATE_VERIFY_FAILED`).

**Decision.** Call `truststore.inject_into_ssl()` at process start. This makes Python verify against the operating system's trust store, which already holds the corporate root. The alternative, disabling verification, was rejected outright.

**Consequences.** Certificates are still fully verified. On Linux and in the container, the system CA bundle is used, which behaves like `certifi`. Step 7 must handle the local Docker dry-run behind the same proxy, for example by mounting the corporate CA as a dev-only option.

---

## ADR-0008: Configuration with pydantic-settings

**Status:** accepted (Step 0)

**Decision.** `app/config.py` is the single source of truth:
- Secrets are `SecretStr`, masked in repr and logs.
- Everything that isn't a secret has a default, so the grader only pastes keys.
- `env_ignore_empty=True`, so `KEY=` means "use the default".
- Validation errors name the variable and never echo its value.

`.env.example` uses unquoted values with comments on their own lines, because `docker run --env-file` keeps quotes literally.

---

## ADR-0009: PyMuPDF (native content order) for PDF parsing; AGPL kept offline

**Status:** accepted (Step 1)

**Context.** The parser needs correct reading order on tables, diagrams and two-column pages, plus per-span typography (font, size, colour) to detect headings, lead-ins, links and callout labels.

**Options.** PyMuPDF, native or geometric (`sort=True`) order; pdfplumber, default or layout; pypdf, plain or layout. All were scored on hand-written reading-order anchors on six hard pages (`scripts/compare_extractors.py`, results in `docs/PARSING_REPORT.md` §2).

**Decision.** PyMuPDF in native content order: 36/36 anchors and 0.5 s for the whole document. pypdf plain ties on order but is 8× slower and gives no styles without custom code. pdfplumber garbles the doubly drawn callout labels ("FFaacceeTTiimmee") and interleaves the two columns on p. 162. PyMuPDF's `find_tables()` was rejected: it loses spaces and invents columns.

**Licensing.** PyMuPDF is AGPL-3.0 (or commercial). It's used only in the offline `ingest` dependency group. It's never installed in the runtime Docker image and never offered as a network service, so the shipped chatbot doesn't link to or distribute AGPL code. The parsed output (`pages.jsonl`) is data, not a derivative of the library. If that ever became a concern, pypdf (BSD-3) gives identical reading order on these pages.

---

## ADR-0010: The printed TOC is the ground truth for the section map; the TOC is not retrievable

**Status:** accepted (Step 1)

**Decision.**
- The TOC (pp. 2–7) defines chapters, appendices and page ranges.
- Font-detected headings are validated against it: all 212 TOC sections appear as headings on their TOC page, and all 36 chapter titles match. The running footer is a second, independent source, and it agrees on all 118 footer pages.
- Content types: `front_matter` (p. 1), `toc` (pp. 2–7), `chapter_opener`, `content`, `legal` (p. 162).
- `toc` and `front_matter` will be **excluded from retrieval**. TOC lines like "45 Chapter 6: Mail" would match every Mail query and would cite p. 4 instead of the real page. They're kept as structured data in `toc.json` (usable for "which pages cover X" logic).
- The legal page is retrievable and tagged `legal`: it is part of the document (e.g. who owns the IOS trademark).
- Page numbering: `page` (used in citations) = printed page = `pdf_page_index` + 1, asserted by a test on every page that prints a number.

---

## ADR-0011: Diagram callout labels and table rows become first-class text elements

**Status:** accepted (Step 1); figure captions from images are a separate experiment in Step 2

**Context.**
- The guide has no printed captions. Callout labels inside figures, such as the p. 8 anatomy diagram and the "Tap to edit." arrows on screenshots, are in the text layer: printed twice (white halo plus dark), wrapped over several lines, and in scrambled order.
- Tables put each column on a separate text line.

**Decision.**
- Halo copies are dropped by colour.
- Wrapped label lines are joined by line pitch, and labels are emitted column by column as one `figure_labels` element ("Figure labels: FaceTime camera; Receiver/front microphone; …") at the figure's position in the reading flow.
- Table rows are rebuilt geometrically into `table_row` elements with `cells`.

**Consequences.** 74 figure-label groups on 63 pages make the screenshots' instructions searchable (e.g. "Swipe left to use the level." on p. 108). Questions about the iPhone's buttons can retrieve p. 8. Column-major order is an approximation of how a reader scans a callout diagram.

---

## ADR-0012: Commit the parsed output

**Status:** accepted (Step 1)

**Decision.** `data/parsed/pages.jsonl` (1.1 MB) and `toc.json` are committed, while caches and renders stay ignored.

**Why.** Reviewers can audit any page's structure without running the AGPL parser. The output is deterministic (no timestamps; parser version and source SHA-256 are recorded). Diffs on re-parse show exactly what a parser change did. During Step 1, a refactor was verified to produce byte-identical output.

---

## ADR-0013: No figure captions or OCR; figure metadata only (Step 2 skipped)

**Status:** accepted (Step 3; decided by the project owner after reviewing the analysis below). To be revisited with evidence in Steps 9–10.

**Context.** The brief proposed vision-model captions and OCR for figures. Measurements:
- 434 image placements become **122 real figures** once fragments are merged into regions. The two iPhone drawings on p. 8 are 88 slices.
- Figures are tiny: median about 106 pt wide, about 140 px in the source image.
- The guide prints no captions, but every figure sits under the instruction it illustrates.
- The text inside figures that matters, the callout labels, is already in the text layer and was extracted in Step 1 (74 groups on 63 pages).

**Options.**
- (a) Caption all 122 figures with a vision model, plus OCR.
- (b) Record figure regions, anchor text and labels only; no model calls.
- (c) Ignore figures.

**Decision.** (b). `ingestion/figures.py` writes `figures.jsonl` (region, anchor instruction, callout labels, `duplicate_of`), and chunks carry `has_figure` / `figure_ids`.

**Why.**
- **Little to gain.** Captions would mostly restate the instruction text that is already indexed, and the high-value figure text (callout labels) is already searchable.
- **Real grounding risk.** A caption is the model's interpretation, not document text. An invented UI detail would be indexed and could be cited as if it were on page N, against the core requirement to answer strictly from the document.
- **Retrieval noise.** Extra caption text dilutes chunk embeddings.
- **Time.** Hours of a timed assessment for an uncertain gain.

**Consequences.**
- Text baked into screenshots (e.g. "Show me great animated films", p. 42) isn't searchable.
- Inline icons (vector drawings, not images) keep losing their names ("tap ✈" becomes "tap.").
- The golden set's figure-dependent questions (Step 8) measure whether this matters. If they fail at retrieval, the affected figures alone get captioned and re-evaluated.

---

## ADR-0014: Structure-aware chunking, 350 BERT tokens including the header, no overlap (starting default)

**Status:** provisional (Step 3). Every alternative is built for the Step 10 comparison.

**Context.**
- The guide's natural units are short: median paragraph 24 tokens, median subsection 173, p90 413.
- The strictest embedding candidates (BGE, E5, GTE) truncate at 512 tokens.
- Answers are usually one task paragraph on one page.

**Decision.**
- Pack whole parsed elements. Chapters and sections are hard boundaries; subsections are soft (they merge forward under 64 tokens).
- Size limit 350 BERT WordPiece tokens for header + body, measured with the committed bert-base-uncased vocabulary. The contextual header path is embedded with the text but not shown to the user.
- No overlap.
- Size splits never end on an intro line, keep table column names, and rebalance tiny tails.
- The naive recursive splitter is kept as a baseline.

**Grid change.** The brief's 300/500/800 became **200/350/500**. An 800-token chunk would be silently truncated by half the candidate models, so the embedding benchmark would compare different texts per model.

**Evidence so far (distributional, not retrieval quality).**
- 336 chunks, median 188 tokens. No chunk crosses a chapter (the naive baseline crosses 7 times).
- Every element lands in a chunk, and every chunk fits the 512-token models.
- Overlap changes only 3 chunks at size 350, because structure-aware chunks rarely split mid-unit.

Retrieval evidence comes in Steps 9–10.

---

## ADR-0015: Chunk payload schema

**Status:** accepted (Step 3)

**Decision.** The payload is documented field by field in `docs/CHUNKING.md` §6. Main choices:
- Deterministic UUIDv5 ids from (config hash, page, embedded text), so ingestion is idempotent.
- `chunk_config_hash` covers the config plus chunker and parser versions, so a parser change can't silently reuse a stale collection.
- `pages` lists every page a chunk touches, and all of them are cited.
- `text` (shown) is separate from `embed_text` (embedded, with the synthetic header).
- `element_refs` keeps traceability to `pages.jsonl`.

**Deviations from the brief.**
- `figure_caption` and `figure_ocr_text` are absent (ADR-0013).
- `content_type` is `text | table | legal` (figures are inline labels, not separate chunks; the TOC is never chunked).
- `subsection_titles` is a list.
- Added `lead_ins` (task titles: keyword anchors and readable citation labels), `partial` and `element_refs`.
