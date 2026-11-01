# iPhone User Guide RAG chatbot

A grounded chatbot that answers questions **only** from the *iOS 7.1 iPhone User Guide* (2014, 162 pages). Every answer shows the page and section it came from. When the answer is not in the guide, the bot says so.

Stack: LangGraph (orchestration) · Streamlit (UI) · Qdrant Cloud (vector store) · OpenRouter (chat and embedding models) · Langfuse (tracing and evaluation, optional).

> **Status:** runs in a single Docker container. A reviewed 80-question golden set is ready; model benchmarking and tuning are future work (see below).

## Quick start

1. `git clone https://github.com/PaulaAdelKamal/Paula_henkel_rag_task.git && cd Paula_henkel_rag_task`
2. `cp .env.example .env`, then fill in `OPENROUTER_API_KEY`, `QDRANT_URL` and `QDRANT_API_KEY`. The Langfuse keys are optional. Write values without quotes: `docker run --env-file` keeps quotes literally.
3. `docker build -t chatbot:1.0 .`
4. `docker run -p 8000:8000 --env-file .env chatbot:1.0`
5. Open http://localhost:8000 and chat.

- **Port:** 8000. Set `APP_PORT` in `.env` and map the same port with `-p` to change it.
- **No ingestion step:** the index is pre-built in Qdrant Cloud, and the app is ready as soon as the container is healthy (Docker `HEALTHCHECK` on `/_stcore/health`).
- **On first load** the app checks OpenRouter, the Qdrant collection and Langfuse. If a required service isn't ready, it shows which one and why, instead of a broken chat.
- **Every answer** ends with **Sources** (page and section). If the guide doesn't cover a question, the bot says so and lists the closest sections it searched.

### Troubleshooting

- **The container shows "healthy" but the browser can't connect.** The port isn't published: `docker ps` shows `8000/tcp` instead of `0.0.0.0:8000->8000/tcp`. Docker Desktop's "Run" button on an image adds neither `-p` nor `--env-file`. Start the container with the command in step 4, or set Host port 8000 and the environment variables under "Optional settings".
- **The page says a required service isn't ready.** The variables from `.env` didn't reach the container (missing `--env-file .env`), or a key or URL is wrong. The page names the failing service.
- **Use `http://localhost:8000`.** Streamlit's log line `0.0.0.0:8000` is the address *inside* the container.

### Running behind a TLS-inspecting proxy (development only)

A corporate network that re-signs HTTPS breaks package downloads during the build and API calls in the container. Export a CA bundle that includes the corporate root, pass it as a build secret, and mount it at run time. Normal environments, including the grader's, don't need this.

```bash
uv run python -m scripts.export_ca_bundle    # -> data/cache/ca-bundle.pem (Windows hosts)
docker build --secret id=ca_bundle,src=data/cache/ca-bundle.pem -t chatbot:1.0 .
docker run -p 8000:8000 --env-file .env -v "$PWD/data/cache/ca-bundle.pem:/etc/ssl/certs/dev-ca-bundle.pem:ro" -e SSL_CERT_FILE=/etc/ssl/certs/dev-ca-bundle.pem chatbot:1.0
```

### Verifying the grader's steps

`scripts/grader_dryrun.sh` (Linux/macOS: git, docker, curl) or `uv run python -m scripts.grader_dryrun` (any OS) clones the committed repository into a temporary directory and runs the five steps above. It waits until the container is healthy, checks that the UI is served, and asks one question through the real UI inside the container, which must return Sources with a page number.

## Models

These are the shipped (free) models. The preferred production combination is under Future work. Exact versions are in `docs/VERSIONS.md`.

| Role | Model |
|---|---|
| Chat | `google/gemma-4-31b-it:free`, falling back to `nvidia/nemotron-3.5-super-vl:free`, then `google/gemma-4-26b-a4b-it:free` (via OpenRouter, free) |
| Embeddings | `nvidia/nemotron-3.5-embed-1b:free` (via OpenRouter, free, 2048-d) |

Only free OpenRouter models are used. A free-tier key allows 50 free-model requests per day (1000 with $10 of credit), so heavy use can hit rate limits.

## Future work

The planned embedding benchmark and optimisation ablations (Steps 9–10) were deferred: they need far more model calls than the free tier allows. The full plan is in `docs/EXPERIMENTS.md`.

- **Compare embedding and answering models** on the reviewed golden set, to find the best combination for this guide. Measure page-level retrieval metrics with confidence intervals, refusal and citation accuracy, and Ragas faithfulness and relevancy with a judge from another model family, as Langfuse experiments.
- **Tune chunking, top-k, hybrid retrieval and the relevance threshold** per embedding model. All 14 chunk configurations are already built.
- **At larger scale** (millions of documents), add a reranker, hybrid dense + sparse retrieval, metadata routing, quantised and sharded indexes, and caching. They aren't needed for one 162-page guide.
- **Preferred production combination (not yet benchmarked):**
  - **embeddings:** hosted **Qwen3-Embedding 4B** (`qwen/qwen3-embedding-4b`, $0.02 per 1M tokens);
  - **answering:** **GPT-6 Luna** (`openai/gpt-6-luna`) or **Claude Haiku 5.5** (`anthropic/claude-haiku-5.5`), both $0.10 / $0.50 per 1M tokens.

  The task isn't demanding: short grounded answers over about five passages, at about $0.0002 per question, so a small, cheap model is the right fit. Switching is configuration plus one ingestion run.

## Development

```bash
uv sync                                    # runtime + dev + ingest groups
uv run python -m pytest                    # unit tests (offline)
uv run python -m scripts.check_connections # verify OpenRouter, Qdrant, Langfuse with your .env
uv run python -m ingestion.parse_pdf        # re-parse the PDF into data/parsed/
uv run python -m ingestion.figures          # figure regions -> data/parsed/figures.jsonl
uv run python -m ingestion.chunking --grid  # chunk every config -> data/chunks/
uv run python -m ingestion.ingest --all --plan   # request budget, no API calls (then --yes)
uv run python -m ingestion.verify_index     # collection health + sanity query + keep-alive
uv run python -m app.cli --debug            # chat in the terminal (answers + Sources)
uv run python -m streamlit run app/ui.py    # web UI on http://localhost:8000
uv run python -m eval.datasets.build        # validate the golden set -> eval/datasets/golden.jsonl
uv run python -m eval.datasets.review html  # review page with the gold PDF pages rendered
uv run python -m pre_commit install --hook-type pre-commit --hook-type pre-push
```

The pre-commit hooks run ruff and gitleaks from Docker images, so Docker must be running (see `docs/DECISIONS.md`, ADR-0003).

## Documentation

- `docs/DECISIONS.md`: decision log (ADRs)
- `docs/PARSING_REPORT.md`: PDF forensics, extractor comparison, parser design and spot check
- `docs/CHUNKING.md`: chunking strategy, grid, distributions and the chunk metadata schema
- `docs/EMBEDDING_CANDIDATES.md`: free embedding models, prefixes, probe and ingestion results
- `eval/datasets/README.md`: golden evaluation set (schema, categories, how gold pages were verified)
- `docs/VERSIONS.md`: pinned library and model versions
- `docs/ARCHITECTURE.md`, `docs/EXPERIMENTS.md`, `docs/INTERVIEW_PREP.md`
