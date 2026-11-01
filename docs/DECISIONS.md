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
- CI runs ruff and pytest natively, and `gitleaks-action` over the full history.

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
