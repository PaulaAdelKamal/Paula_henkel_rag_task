# Pinned versions

`uv.lock` is the source of truth (exact versions and hashes for every package). This page records the versions that matter for the interview, and when they were checked.

## Toolchain

| Component | Version | Notes |
|---|---|---|
| Python | 3.14 (3.14.2 locally) | ADR-0001 |
| uv | 0.9.18 locally | lock format is forward compatible |
| Docker base image | `python:3.14-slim` = 3.14.10 on Debian trixie, digest `sha256:cd89c30e…` | builder + runtime (ADR-0025) |
| uv in the image | `ghcr.io/astral-sh/uv:0.13.6`, digest `sha256:af22a52f…` | builder stage only |

## Runtime dependencies (shipped in the image)

| Package | Locked version | Role |
|---|---|---|
| langchain | 1.4.7 | orchestration primitives |
| langchain-core | 1.7.3 | (transitive) messages, runnables |
| langchain-openai | 1.7.3 | OpenAI-compatible client pointed at OpenRouter |
| langgraph | 1.2.19 | RAG graph and checkpointer memory |
| openai | 3.42.0 | (transitive) HTTP client for OpenRouter |
| qdrant-client | 1.20.0 | vector store client |
| langfuse | 5.1.0 | tracing, datasets, experiments (SDK v5) |
| streamlit | 1.66.1 | chat UI (ADR-0002) |
| pydantic | 2.14.2 | (transitive) |
| pydantic-settings | 2.16.2 | typed configuration |
| httpx | 0.28.1 | direct HTTP calls (OpenRouter `/key`, model listing) |
| truststore | 0.10.5 | OS trust store for TLS (ADR-0007) |
| pypdfium2 | 5.14.0 | renders the cited page in the UI (BSD-3/Apache-2.0) |

## Dev and offline groups (never in the image)

| Package | Locked version | Group |
|---|---|---|
| ruff | 0.17.3 | dev (CI); `ghcr.io/astral-sh/ruff:0.17.3` locally |
| pytest | 9.2.0 | dev |
| pre-commit | 4.7.0 | dev |
| pre-commit-hooks | 6.0.0 | dev |
| detect-secrets | 1.5.0 | dev |
| gitleaks | 8.30.1 | `zricethezav/gitleaks:v8.30.1` Docker image, locally and in CI |
| pymupdf | 2.0 | ingest (AGPL-3.0, offline only); chosen parser (ADR-0009) |
| pdfplumber | 0.11.10 (pdfminer.six 20260107) | ingest (MIT); comparison only |
| pypdf | 6.21.4 | ingest (BSD-3-Clause); comparison only, licence fallback |
| langchain-text-splitters | 1.1.3 | ingest (MIT); naive baseline + oversized-element splits |
| tokenizers | 0.23.3 | ingest (Apache-2.0); BERT WordPiece token counts |
| matplotlib | 3.11.2 | ingest; chunk distribution plots |
| bert-base-uncased vocab | 30,522 tokens | committed asset (Apache-2.0), `ingestion/assets/` |

## CI actions (pinned by commit SHA)

| Action | Tag | SHA |
|---|---|---|
| actions/checkout | v7.0.1 | 3d3c42e5aac5ba805825da76410c181273ba90b1 |
| astral-sh/setup-uv | v10.3.3 | 68559e99cc807c1eb7e9238fd43a15bba460eec2 |

## Models (shipped: free OpenRouter models; the benchmark is deferred, see docs/EXPERIMENTS.md)

Checked against the live OpenRouter listings on 2026-11-01. Full registry and probe results: `docs/EMBEDDING_CANDIDATES.md`.

| Role | Slug | Status |
|---|---|---|
| Chat | `google/gemma-4-31b-it:free` | provisional primary (often rate-limited upstream) |
| Chat fallback 1 | `nvidia/nemotron-3.5-super-vl:free` | answered all Step 5 gate questions |
| Chat fallback 2 | `google/gemma-4-26b-a4b-it:free` | |
| Embedding | `nvidia/nemotron-3.5-embed-1b:free` (2048-d) | provisional default, ingested |
| Embedding | `liquid/lfm-2.5-embedding-350m:free` (1024-d) | ingested (benchmark candidate) |
| Embedding | `nvidia/nemotron-3.5-embed-8b:free` (4096-d) | shortlisted, deferred (endpoint fails on batches) |
| Vision captioning | not chosen yet | Step 2 |
| Ragas judge | not chosen yet (must be a different family from chat) | Step 9 |

### Preferred production combination (owner's choice, not benchmarked; ADR-0027)

| Role | Slug | Price on OpenRouter (2026-11-01) |
|---|---|---|
| Embedding | `qwen/qwen3-embedding-4b` (32k context) | $0.02 per 1M tokens |
| Answering | `openai/gpt-6-luna` | $0.10 in / $0.50 out per 1M tokens |
| Answering (alternative) | `anthropic/claude-haiku-5.5` | $0.10 in / $0.50 out per 1M tokens |
