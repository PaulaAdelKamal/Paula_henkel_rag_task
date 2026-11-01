# iPhone User Guide RAG chatbot

A grounded chatbot that answers questions **only** from the *iOS 7.1 iPhone User Guide* (2014, 162 pages). Every answer shows the page and section it came from. When the answer is not in the guide, the bot says so.

Stack: LangGraph (orchestration) · Streamlit (UI) · Qdrant Cloud (vector store) · OpenRouter (chat and embedding models) · Langfuse (tracing and evaluation, optional).

> **Status:** Step 3, parsing and chunking done (`data/parsed/`, `data/chunks/`). The chatbot is not runnable yet.

## Quick start (final form, available from Step 7)

1. `git clone <repo>`
2. `cp .env.example .env` and fill in the keys
3. `docker build -t chatbot:1.0 .`
4. `docker run -p 8000:8000 --env-file .env chatbot:1.0`
5. Open http://localhost:8000

**Port:** 8000. The index is pre-built in Qdrant Cloud, so there is no ingestion step.

## Models

Provisional until the embedding benchmark (Step 9) and the ablations (Step 10). Exact versions are in `docs/VERSIONS.md`.

| Role | Model |
|---|---|
| Chat | `openai/gpt-4.1-mini` (via OpenRouter) |
| Embeddings | `qwen/qwen3-embedding-8b` (via OpenRouter) |

## Development

```bash
uv sync                                    # runtime + dev + ingest groups
uv run python -m pytest                    # unit tests (offline)
uv run python -m scripts.check_connections # verify OpenRouter, Qdrant, Langfuse with your .env
uv run python -m ingestion.parse_pdf        # re-parse the PDF into data/parsed/
uv run python -m ingestion.figures          # figure regions -> data/parsed/figures.jsonl
uv run python -m ingestion.chunking --grid  # chunk every config -> data/chunks/
uv run python -m pre_commit install --hook-type pre-commit --hook-type pre-push
```

The pre-commit hooks run ruff and gitleaks from Docker images, so Docker must be running (see `docs/DECISIONS.md`, ADR-0003).

## Documentation

- `docs/DECISIONS.md`: decision log (ADRs)
- `docs/PARSING_REPORT.md`: PDF forensics, extractor comparison, parser design and spot check
- `docs/CHUNKING.md`: chunking strategy, grid, distributions and the chunk metadata schema
- `docs/VERSIONS.md`: pinned library and model versions
- `docs/ARCHITECTURE.md`, `docs/EXPERIMENTS.md`, `docs/INTERVIEW_PREP.md`
