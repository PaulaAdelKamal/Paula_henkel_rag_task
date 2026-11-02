# iPhone User Guide RAG chatbot (full README)

> The short [`README.md`](README.md) has just the run steps, port, variables and models. This file explains how the project works. The design notes it mentions (decision log, parsing and chunking reports, failure analysis) are kept by the author outside the repository; every file in the repository is listed in [`file_inventory.csv`](file_inventory.csv).

A grounded chatbot that answers questions **only** from the *iOS 7.1 iPhone User Guide* (Apple, 2014, 162 pages).
- Every answer ends with **Sources**: the page and section it came from.
- If the guide doesn't cover a question, the bot says so and lists the closest sections it searched, clearly labelled as not being sources.
- It keeps the conversation, so follow-ups like "how do I turn it on?" work.

**Stack:** LangGraph (orchestration) · Streamlit (UI) · Qdrant Cloud (vector store, free tier) · OpenRouter (embedding and chat models, free tier) · Langfuse Cloud (tracing, optional) · one Docker container.

## Quick start

1. `git clone https://github.com/PaulaAdelKamal/Paula_henkel_rag_task.git && cd Paula_henkel_rag_task`
2. `cp .env.example .env`, then fill in `OPENROUTER_API_KEY`, `QDRANT_URL` and `QDRANT_API_KEY`. The Langfuse keys are optional. Write values without quotes: `docker run --env-file` keeps quotes literally.
3. `docker build -t chatbot:1.0 .`
4. `docker run -p 8000:8000 --env-file .env chatbot:1.0`
5. Open **http://localhost:8000** and chat.

- **Port:** 8000. Set `APP_PORT` in `.env` and map the same port with `-p` to change it.
- **No ingestion step:** the index is pre-built in Qdrant Cloud. The container is ready when its Docker `HEALTHCHECK` (on `/_stcore/health`) reports healthy, usually within a minute.
- **On first load** the app checks OpenRouter (key and models), the Qdrant collection (exists, non-empty, right dimension) and Langfuse. If a required service isn't ready, it names it instead of showing a broken chat.

### Troubleshooting

- **"Healthy" in Docker but the browser can't connect.** The port isn't published: `docker ps` shows `8000/tcp` instead of `0.0.0.0:8000->8000/tcp`. Docker Desktop's "Run" button adds neither `-p` nor `--env-file`; use the command in step 4.
- **The page says a required service isn't ready.** `.env` didn't reach the container (missing `--env-file .env`), or a key or URL is wrong. The page names the service.
- **"The AI service is busy or has reached its free usage limit".** Free OpenRouter models are rate-limited: a key without credit allows 50 free requests per day. Try again later, or use a key with credit.
- Browse to `http://localhost:8000`. The `0.0.0.0:8000` in Streamlit's log is the address *inside* the container.

## How it works

```
offline (ingestion/, not in the image)
  PDF ─> parse_pdf ─> structured pages (TOC-anchored sections, footers stripped,
                      lead-ins, tables, diagram labels, cross-references)
      ─> chunking ─> 336 structure-aware chunks (≤350 tokens, section-path header)
      ─> ingest ───> OpenRouter embeddings ─> Qdrant Cloud (one collection per model)

runtime (app/, in the image)
  Streamlit ─> LangGraph:  condense follow-up ─> retrieve top 5 ─> relevance gate
                           ─> generate {answerable, answer, used_sources} ─> validate
                           ─> answer + Sources  |  "not in the guide" + closest sections
             ─> Langfuse trace per turn (+ thumbs up/down as a score)
```

- **Citations can't be invented.** The model sees the sources as `[1]…[5]` with section names but **no page numbers**, and it returns the numbers it used. Code drops numbers that don't exist and renders page and section from the chunk metadata. "Answerable" with no valid source becomes a refusal.
- **Prompt injection.** Retrieved text and user text are declared data. Structurally, "cite page 12" can't produce a page-12 citation, and an answer always needs a retrieved source.
- **Memory.** A LangGraph checkpointer keyed by the browser session; follow-ups are rewritten into standalone questions from the last three turns.
- **Failures** (quota exhausted, provider or Qdrant outage) give a plain message, never a stack trace.

Details: the author's architecture notes (outside the repository) (graph diagram, grounding guarantees, Langfuse trace layout).

## Configuration

Every variable is in `.env.example`, with a one-line description. Only the first three are required.

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `OPENROUTER_API_KEY` | yes | — | Chat model and query embeddings |
| `QDRANT_URL` | yes | — | Qdrant Cloud cluster URL |
| `QDRANT_API_KEY` | yes | — | Qdrant Cloud API key |
| `CHAT_MODEL` | no | `google/gemma-4-31b-it:free` | Answering model |
| `CHAT_FALLBACK_MODELS` | no | `nvidia/nemotron-3.5-super-vl:free,google/gemma-4-26b-a4b-it:free` | Tried in order on errors or rate limits; paid models are ignored |
| `EMBEDDING_MODEL` | no | `nvidia/nemotron-3.5-embed-1b:free` | Must match an ingested collection |
| `QDRANT_COLLECTION` | no | derived | Override of `iphone_guide__<model>__<chunk_hash>` |
| `CHUNK_CONFIG_HASH` | no | `edb94b731b` | Chunk configuration the index was built with |
| `TOP_K` | no | `5` | Chunks retrieved per question |
| `RELEVANCE_THRESHOLD` | no | `0.0` (off) | Optional score pre-filter |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | no | empty | Tracing; empty disables it |
| `LANGFUSE_BASE_URL` | no | `https://cloud.langfuse.com` | Langfuse region |
| `APP_ENV` / `APP_PORT` / `LOG_LEVEL` | no | `dev` / `8000` / `INFO` | Trace tag, port, log verbosity |
| `CACHE_DIR` | no | `data/cache` | Embedding cache (offline tools) |

Settings are validated at startup (pydantic-settings). Errors name the variable, never its value. Keys are `SecretStr` and are redacted from all messages. `.env` is ignored by git and Docker; secret scanners run on every commit and push, and in CI.

## Models

| Role | Model (OpenRouter slug) | Notes |
|---|---|---|
| Chat | `google/gemma-4-31b-it:free` → `nvidia/nemotron-3.5-super-vl:free` → `google/gemma-4-26b-a4b-it:free` | Free fallback chain; temperature 0; structured answer by function calling |
| Embeddings | `nvidia/nemotron-3.5-embed-1b:free` (2048-d) | Prefixes `query: ` / `passage: ` |
| Also ingested | `liquid/lfm-2.5-embedding-350m:free` (1024-d) | Prefixes `query: ` / `document: ` |

The models were chosen under a free-models-only constraint and checked against OpenRouter's live listings on 2026-11-01. Pinned library versions and base images are in the author's versions notes (outside the repository).

## Ingestion, chunking and the vector store

- **Parsing** (`ingestion/parse_pdf.py`, PyMuPDF in native reading order, chosen by measuring six extractor configurations; the author's parsing report notes (outside the repository)):
  - The printed TOC is the ground truth for chapters and sections; all 212 TOC sections were found as headings on their pages.
  - Running footers are stripped and harvested as metadata; printed page = PDF page.
  - Bold task lead-ins, tables, and diagram callout labels (printed twice in the PDF, de-duplicated) become text elements.
  - 165 "See … on page N" cross-references are detected, and 164 of them resolve to their target section.
- **Chunking** (`ingestion/chunking.py`, the author's chunking notes (outside the repository)):
  - Structure-aware: whole paragraphs, list items and table rows are packed into chunks of at most **350 BERT tokens, including a header** such as "iPhone User Guide > Chapter 3: Basics > Use apps > Multitasking".
  - A chunk never crosses a chapter or section. Small subsections merge forward, and continued tables repeat their column names.
  - The limit fits the strictest embedding models (512 tokens). The guide's natural units are short how-to subsections (median 173 tokens), so most stay whole.
  - Result: 336 chunks.
- **Vector store:** Qdrant Cloud, cosine distance, one collection per (embedding model, chunk config). Point ids are deterministic UUIDv5s, so re-ingesting only adds or removes what changed.
  - Payload fields: `page`, `pages`, `pdf_page_index`, `chapter_number`, `chapter_title`, `section_title`, `subsection_titles`, `section_path`, `content_type` (text/table/legal), `lead_ins`, `has_figure`, `figure_ids`, `xrefs`, `text`, `header`, token counts, `chunk_config_hash`, `embedding_model`, `ingested_at`.
  - Indexed fields: `page`, `pages`, `chapter_number`, `content_type`, `has_figure`.
- **Re-ingest:** `uv run python -m ingestion.parse_pdf && uv run python -m ingestion.figures && uv run python -m ingestion.ingest --all --plan`, then rerun with `--yes`.

## Evaluation

`eval/datasets/golden.jsonl`, also uploaded to the Langfuse dataset `golden`, holds **80 questions reviewed and accepted by the project owner**:
- 9 categories: single facts, how-tos, multi-page, figure-dependent, cross-references, multi-turn, iPhone-model traps, 13 unanswerable and 6 adversarial;
- every gold page is derived from a verbatim evidence quote that code checks on the page.

Model benchmarking on this set is future work (below). The shipped behaviour was verified by:
- an 8-question manual gate in the terminal (answers, follow-ups, refusals, injection);
- the in-container smoke test;
- 110 offline tests (graph routing, citations, UI, parsing, chunking, datasets).

Red-team policy and results: the author's failure analysis notes (outside the repository).

## Observability

With Langfuse keys, every chat turn is one trace:
- a `rag-turn` root span with the graph nodes and the LLM generation nested under it;
- session id, environment, release and tags (embedding model, chunk config, prompt version, git sha);
- root metadata: the decision, the rewritten question, the model that actually answered, retrieved chunk ids with pages and scores, and cited pages.

Thumbs up/down in the UI become a `user-feedback` score on that trace. Without keys, tracing is a no-op.

## Limitations

- Inline icons are vector graphics, so "tap ✈" reads as "tap."; text inside screenshots isn't searchable (figure captioning was skipped).
- Defaults (chunking, models, relevance threshold) are reasoned, not benchmarked.
- Free models rate-limit, and answers may come from a fallback model.
- Conversation memory is in-process (a restart forgets it).
- The Qdrant free cluster is suspended after about a week idle. The `qdrant-keepalive` workflow pings it once the repository secrets `QDRANT_URL` and `QDRANT_API_KEY` are set.

## Future work

The planned embedding benchmark and optimisation ablations were deferred (they need far more model calls than the free tier allows). The full plan is in the author's evaluation plan notes (outside the repository).

- **Compare embedding and answering models** on the reviewed golden set, to find the best combination for this data:
  - page-level retrieval metrics with confidence intervals;
  - refusal and citation accuracy;
  - Ragas faithfulness and relevancy with a judge from another model family;
  - all run as Langfuse experiments.
- **Tune** chunking (all 14 configurations are already built), top-k, hybrid retrieval, and the relevance threshold per embedding model.
- **At larger scale** (millions of documents): add a reranker, hybrid dense + sparse retrieval, metadata routing, quantised and sharded indexes, and caching.
- **Preferred production combination (not yet benchmarked):**
  - **embeddings:** hosted **Qwen3-Embedding 4B** (`qwen/qwen3-embedding-4b`, $0.02 per 1M tokens);
  - **answering:** **GPT-6 Luna** (`openai/gpt-6-luna`) or **Claude Haiku 5.5** (`anthropic/claude-haiku-5.5`), both $0.10 / $0.50 per 1M tokens.

  The task isn't demanding: short grounded answers over about five passages, at about $0.0002 per question, so a small, cheap model is the right fit. Switching is configuration plus one ingestion run.

## Development

```bash
uv sync                                      # runtime + dev + ingest dependency groups
uv run python -m pytest                      # 110 offline tests
uv run python -m scripts.check_connections   # OpenRouter key and models, Qdrant collection, Langfuse
uv run python -m app.cli --debug             # terminal chat (answers + Sources + debug line)
uv run python -m streamlit run app/ui.py     # web UI on http://localhost:8000
uv run python -m ingestion.verify_index      # collection health + sanity query (keeps the cluster active)
uv run python -m scripts.red_team            # live red-team (about 22 free requests)
uv run python -m eval.datasets.build         # validate the golden set
uv run python -m eval.datasets.review html   # review page with the gold PDF pages rendered
uv run python -m pre_commit install --hook-type pre-commit --hook-type pre-push
```

Pre-commit runs detect-secrets, gitleaks, ruff and a lockfile check. CI runs lint, tests, a full-history gitleaks scan and a Docker build with a boot check.

## Repository layout

```
app/          runtime (in the image): config, embeddings, vectorstore, graph, llm, prompts,
              citations, health, observability, pages, ui (Streamlit), cli
ingestion/    offline: parse_pdf, figures, chunking, ingest, verify_index (+ BERT vocabulary)
eval/datasets golden set: schema, builder/validator, review tools, Langfuse upload
scripts/      connection check, grader dry-run (.sh/.py) + in-container smoke test, red team
tests/        offline pytest suite (stubs, mocked HTTP, in-memory Qdrant, Streamlit AppTest)
data/         source PDF and parsed pages / TOC / figures (all committed)
Dockerfile · .env.example · pyproject.toml / uv.lock · .github/workflows (ci, qdrant-keepalive)
README.md (short) · README_FULL.md (this file) · file_inventory.csv (every file, one line each)
```
