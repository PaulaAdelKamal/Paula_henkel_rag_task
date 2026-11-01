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

---

## ADR-0016: Free OpenRouter embedding models only; three shortlisted

**Status:** accepted (Step 4; the free-only constraint and the 3-model shortlist were set by the project owner)

**Context.** The OpenRouter key is on the free tier: 50 free-model requests per day, shared by embeddings and the free chat model. Five free embedding models exist (Nemotron 3.5 1B and 8B, Nemotron 3 1B, Llama Nemotron Embed VL 1B v2, LFM 2.5 350M). One ingestion takes about 6 batched requests per model.

**Decision.**
- All five go in the registry, with prefixes and dimensions from their model cards.
- Three were shortlisted for ingestion:
  - Nemotron 3.5 1B, which is also the runtime default;
  - Nemotron 3.5 8B, the large end;
  - LFM 2.5 350M, the small end, with a different architecture and a 512-token limit.
- Nemotron 3.5 1B and LFM are ingested. Nemotron 3.5 8B is deferred because its hosted endpoint returns 502 on every batched request (single inputs work), which would cost more than a day's quota.

**Consequences.**
- The benchmark currently has two models, both returning the right page at rank 1 on the sanity query.
- Results from free endpoints depend on availability and rate limits, so they must be documented as such.
- The model choice is a one-variable change through the registry, but each model needs its own collection.
- The 50/day cap must be resolved before evaluation (Step 8): either add $10 of credit for 1000/day, still on free models, or design evaluation around the cap.

---

## ADR-0017: Custom LangChain `Embeddings` client instead of `OpenAIEmbeddings`

**Status:** accepted (Step 4)

**Context.** `langchain_openai.OpenAIEmbeddings` with `check_embedding_ctx_length=False` can call OpenRouter, but:
- it adds no per-model query/document prefixes (`embed_query` is just `embed_documents([text])[0]`);
- it exposes no token usage;
- it can't tell a per-minute rate limit (retry) from an exhausted daily quota (stop);
- it has no persistent cache.

**Decision.** `app/embeddings.py` implements LangChain's `Embeddings` interface over `httpx` (about 150 lines). It provides:
- per-model prefixes from the `EmbeddingSpec` registry;
- batching that halves the batch size on size errors;
- retries with backoff on 429/5xx, honouring `Retry-After`;
- an immediate `QuotaExhausted` on the daily cap;
- detection of errors returned inside a 200 response;
- a dimension check on every response;
- L2 normalisation;
- usage and request counters;
- an optional SQLite cache keyed by the hash of (model, text).

It plugs into LangChain and LangGraph like any other `Embeddings`.

**Consequences.** More code to own, but it's covered by 11 offline tests against a mocked transport. The quota-aware behaviour is what made it possible to ingest two models in 12 requests without wasting any.

---

## ADR-0018: Idempotent ingestion by deterministic ids

**Status:** accepted (Step 4)

**Decision.**
- Point ids are the chunks' UUIDv5s.
- Ingestion computes the difference between the chunk ids and the collection's ids: it embeds and upserts only the missing ones and deletes stale ones. A changed chunk has a new id, so the old version is deleted and the new one added.
- Vectors are cached by content hash.
- A no-op run makes no API calls and keeps the report of the run that actually built the collection.

**Evidence.** A second run on both collections made 0 embedding requests, 0 upserts and 0 deletes.

---

## ADR-0019: RAG graph shape and budget

**Status:** accepted (Step 5)

**Decision.** A LangGraph state machine: `prepare` -> `condense` (only with history) -> `retrieve` -> relevance gate -> `generate` -> validation -> answer or refusal. Every collaborator (retriever, answerer, condenser) is injected, so the real graph is unit-tested with stubs at no cost (16 tests). Conversation memory uses a checkpointer keyed by `thread_id`.

**Why.**
- At most one embedding and one chat request per question, plus one for a follow-up rewrite, which matters under a 50-request daily free cap.
- The first turn skips the rewrite.
- The gate can end a turn before any chat call.
- Failures degrade instead of crashing: a failed rewrite falls back to the raw question, and a failed answer becomes a clear message that still lists the searched sections.

**Rejected.**
- A separate LLM "relevance grader" node: it adds one request per turn, and the structured `answerable` flag already makes that decision.
- Agentic tool-calling loops: unbounded cost, and harder to guarantee citations.

---

## ADR-0020: Citations from metadata, source numbers from the model; refusal wording

**Status:** accepted (Step 5)

**Decision.**
- The model sees numbered sources without page numbers and returns `used_sources`.
- Code validates the numbers (invented ones are dropped) and renders citations from chunk metadata as `Page 22 · Chapter 3: Basics > Use apps > Multitasking`; ranges appear as `Pages 12–13`.
- `answerable` without a valid source becomes a refusal.
- The refusal wording is "I couldn't find the answer to that in the iPhone User Guide for iOS 7.1, so I can't answer it. I only answer from that guide."
- Refusals are followed by "**Closest sections searched (not sources of an answer)**" with the top 3 distinct sections in relevance order.

**Why.**
- A page number typed by the model can be wrong; a page number read from metadata can't.
- The brief requires page/section info on every response, refusals included, and labelling the list as non-sources keeps that honest.

---

## ADR-0021: Free chat model with an OpenRouter fallback chain

**Status:** accepted (Step 5)

**Context.** `google/gemma-4-31b-it:free` has a single upstream provider (Google AI Studio) and returned 429 "Provider returned error" on every answer call during the Step 5 checks.

**Decision.**
- Send OpenRouter's `models` list, which is tried in order on any error, rate limits included: `google/gemma-4-31b-it:free` -> `nvidia/nemotron-3.5-super-vl:free` -> `google/gemma-4-26b-a4b-it:free`.
- The list is configured with `CHAT_FALLBACK_MODELS`, and non-free entries are ignored, so a fallback can never cost money.
- The answering model is recorded in `model_used`.
- The answer is requested as a function call, because Gemma's free endpoint supports tools but not strict JSON-schema output. JSON in the text is a parse fallback.

**Consequences.** Answers may come from different models from turn to turn. All 8 Step 5 gate answers came from Nemotron 3.5 Super VL. The chat model comparison in Step 10 decides the primary, and Langfuse (Step 6) will show which model answered each turn.

---

## ADR-0022: The relevance threshold is a pre-filter, not the decision

**Status:** provisional (Step 5; calibrated in Step 10)

**Evidence.** In the manual gate, the unanswerable "How do I set up Face ID?" had a top cosine of 0.483, while the answerable "How do I take a screenshot?" had 0.318.

**Decision.**
- The answerable/unanswerable decision is the model's structured `answerable` flag, backed by code validation of its sources.
- `RELEVANCE_THRESHOLD` (default 0.0, i.e. off) only short-circuits clearly off-topic queries to save a request.
- It will be calibrated per embedding model from the score distributions of answerable and unanswerable evaluation questions.

---

## ADR-0023: Streamlit UI: visible Sources, progress streaming, page preview

**Status:** accepted (Step 6)

**Decisions.**
- **Sources inside the message.** The **Sources** block is part of the message text itself, not a hidden element, so it's visible in every rendering and in exports. The "Cited passages" expander adds the cited text, and a toggle renders the cited page from the bundled PDF.
- **Progress streaming, not token streaming.** The answer is a structured function-call output (`answerable`, `answer`, `used_sources`), and validating it needs the complete object. Streaming partial tool-call JSON would show text before its sources are validated. So the UI streams *progress* node by node and shows the validated answer in one piece. Faking a typewriter effect was rejected as cosmetic.
- **Page rendering with pypdfium2** (BSD-3/Apache-2.0, 0.05 s per page). `st.pdf` can't jump to a page, and PyMuPDF (AGPL) must stay out of the runtime image.
- **Startup self-check** before the first message, cached per process. A failing required service shows a readable error screen.
- **Starter questions** are pills consumed once by a callback; a selected pill would otherwise re-ask on every rerun.

**Bug found by the live run.** The SQLite embedding cache was created on one thread and used on Streamlit/LangGraph worker threads (`ProgrammingError`). It now opens with `check_same_thread=False` behind a lock, with a regression test.

---

## ADR-0024: Langfuse tracing: one trace per turn, feedback as a score, never blocking

**Status:** accepted (Step 6)

**Decisions.**
- `Langfuse.create_trace_id()` before each turn, then a `rag-turn` root span via `start_as_current_observation(trace_context=...)`, with the LangChain `CallbackHandler` for the graph's LLM calls.
- Trace attributes go in through `langfuse_session_id` / `langfuse_user_id` / `langfuse_tags` config metadata.
- Retrieval results, the decision and the answering model are written onto the root span.
- Thumbs up/down call `create_score(trace_id, name="user-feedback", value=1|0)`.
- Every tracing call is wrapped: without keys or on any error, it's a no-op.

**Pitfall found and fixed.** The startup check validated the keys by creating an SDK client and calling `shutdown()`. Langfuse v5 shares one client per public key in the process, so this also shut down the tracer's client, and the feedback score was dropped ("Langfuse client has been shut down"). The health check now calls the REST API directly (`GET /api/public/projects` with basic auth) and never touches the SDK singleton.

**Evidence.** A live trace showed 21 observations, retrieval metadata on `rag-turn`, the answering model, and `user-feedback = 1.0` attached to the trace. With the keys removed, answering still worked.

---

## ADR-0025: Container build and run

**Status:** accepted (Step 7)

**Decisions.**
- **Multi-stage build:** a uv builder installs `[project.dependencies]` from the frozen lock (`--no-default-groups`, so no dev or ingest groups); the runtime stage copies only the venv and the four runtime paths.
- **Pinned bases:** Python 3.14.10 slim (trixie) and uv 0.13.6, by tag and digest, for reproducible builds.
- **Non-root user, healthcheck, port:**
  - The container runs as `app`.
  - `HEALTHCHECK` probes Streamlit's `/_stcore/health` with Python, since there's no curl in the slim image.
  - The default port is 8000, and `APP_PORT` from `--env-file` is honoured through a shell-form `CMD` that `exec`s Streamlit, so it stays PID 1 and receives signals.
- **Telemetry off:** Streamlit usage stats (config file and env var).
- **Behind a TLS-inspecting proxy (dev only):** an optional BuildKit secret `ca_bundle` sets `SSL_CERT_FILE` for uv during the build, and a mounted bundle plus `SSL_CERT_FILE` covers run time (`truststore` reads the system store). The grader's plain build ignores the secret, which is `required=false`.

**Verification.**
- `scripts/grader_dryrun.py` passed from a clean clone, smoke question included.
- CI builds the image without secrets and asserts:
  - no `.env`, no offline code or tests, no PyMuPDF;
  - a non-root user;
  - with no keys at all, the container stays up, is healthy and serves the UI, which shows the readable startup-error screen.
- CI also runs `scripts/grader_dryrun.sh` end to end on Linux, with the smoke question skipped since CI has no keys. ShellCheck is clean.

**Rejected.**
- Alpine: Python wheels such as pyarrow and pypdfium2 are glibc builds.
- Baking a `.env` or keys into the image.
- Downloading models at container start: all models are API calls, so the container is ready as soon as Streamlit is.

---

## ADR-0026: Golden set drafted by Claude with machine-verified evidence; synthetic set deferred

**Status:** accepted (Step 8). Gate passed on 2026-11-01: the project owner reviewed all 80 items and accepted them unchanged.

**Context.** The brief asks for 60–100 hand-verified questions drafted by an LLM, plus a synthetic set from the Ragas test-set generator. The OpenRouter key allows 50 free requests per day.

**Decisions.**
- **Drafting:** the 80 golden candidates were drafted by Claude (Opus 5.5, in Claude Code), reading `data/parsed/pages.jsonl` directly. That's an LLM draft, as the brief asks, at zero OpenRouter cost, with direct control over category balance and hard negatives.
- **Evidence, not page numbers.** Each answerable item carries verbatim **evidence quotes**. `eval/datasets/build.py` checks every quote on its page (normalising case, whitespace and quote marks) and **derives** gold pages and gold sections from where the quotes are. No page number or section is typed by hand, so the human review checks meaning and the best page, not transcription.
- **Coverage:** 9 categories, including figure-dependent questions (facts that exist only in callout labels), cross-references, multi-turn with history, model near-misses, 13 unanswerable questions with hard negatives, and 6 adversarial questions. 21% must be refused.
- **Review:** an HTML page with the rendered gold pages, a CSV round trip and an interactive CLI. `review_status` is kept per item, and edits mark an item `edited`.
- **Langfuse:** an idempotent upload to the dataset `golden`, with deterministic item ids. Rejected items are archived, not deleted. This uses Langfuse keys only.
- **Synthetic set deferred.** Ragas builds a knowledge graph over every chunk before generating (hundreds of LLM calls), which doesn't fit the free quota. It's optional for the brief's metrics and will run if the quota allows. The golden set doesn't depend on it.

**Consequences.**
- A Claude-drafted set may share Claude's phrasing habits. The human review, the adversarial and hard-negative items, and the multi-turn paraphrases mitigate this.
- The synthetic set would add scale and a different question distribution once generated.

---

## ADR-0027: Steps 9–10 (benchmark and ablations) deferred to future work; preferred production models

**Status:** accepted (decided by the project owner, 2026-11-01)

**Context.** The planned embedding benchmark, answering-model comparison, Ragas evaluation and ablations need hundreds to thousands of model calls. The project runs on free OpenRouter models capped at 50 requests per day. Free endpoints are also noisy (Gemma was rate-limited throughout Step 5, and Nemotron 3.5 8B embeddings fail on batches), so a comparison on them wouldn't be fair either.

**Decision.**
- Don't execute Steps 9 and 10 in this submission.
- Document them as future work in `docs/EXPERIMENTS.md`: protocol, metrics, candidates, ablations, and the techniques to add at scale (reranker, hybrid retrieval, metadata routing, quantisation, caching).
- Record the owner's preferred production combination, untested here:
  - **embeddings:** Qwen3-Embedding 4B, hosted ($0.02 per 1M tokens);
  - **answering:** GPT-6 Luna or Claude Haiku 5.5 ($0.10 in / $0.50 out per 1M tokens, both with strict structured outputs).
- The rationale is price: the task is undemanding, short grounded answers over about five passages, at about $0.0002 per question.

**Consequences.**
- There are no benchmark numbers. The provisional defaults (Nemotron 3.5 1B embeddings, the free Gemma → Nemotron chat chain, chunk config `edb94b731b`, relevance threshold 0) stay the shipped configuration, justified by design reasoning and the manual gates rather than by measurement. ADR-0014 and ADR-0022 remain *provisional*.
- Everything the experiments need already exists: the reviewed golden set in Langfuse, the 14 chunk configurations, the embedding registry, idempotent ingestion, model choice via environment variables, and per-turn traces.
- The plan's Step 9/10 tags are intentionally absent, as Step 2's is.

---

## ADR-0028: Hardening: classified errors, quiet logs, no dead configuration, cluster keep-alive

**Status:** accepted (Step 11)

**Decisions.**
- **Errors become plain messages, by family.** A failure in retrieval or generation is classified:
  - **rate limit:** the free quota is exhausted, or the provider returns 429 after the fallback chain;
  - **unavailable:** network, Qdrant or provider outage;
  - **unverifiable answer:** the model didn't return the structured answer.

  Each gets its own user-facing message, and the exception detail is kept in state for traces and logs only. Before this, a quota error reached the UI as "Something went wrong (QuotaExhausted)" and crashed the CLI.
- **Input limits:** questions are truncated at 2,000 characters, and the chat box shows the limit.
- **Logging:** the app logs at `LOG_LEVEL`. HTTP client libraries (httpx, urllib3, openai, langfuse, qdrant_client) are capped at WARNING so request URLs aren't logged, unless `LOG_LEVEL=DEBUG`.
- **Dead configuration removed:** `VISION_MODEL`, `JUDGE_MODEL`, `EVAL_GEN_MODEL` and `MAX_SPEND_USD` belonged to the skipped steps (ADR-0013, ADR-0027), and the unimplemented `eval/` stubs are deleted. `.env.example` lists only what the code reads.
- **Keep-alive:** a scheduled GitHub Action pings the Qdrant cluster every three days, so the free cluster isn't suspended during grading. It needs the repository secrets `QDRANT_URL` and `QDRANT_API_KEY`, and skips without them.
- **Red-team policy:** the decided behaviour for 11 adversarial and unusual input classes is in `docs/FAILURE_ANALYSIS.md`. `scripts/red_team.py` checks them live, and its verdict logic is unit-tested.
