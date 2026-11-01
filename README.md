# iPhone User Guide RAG chatbot

Chatbot that answers questions **only** from the *iOS 7.1 iPhone User Guide* (Apple, 2014). Every answer shows the **page and section** it came from. If the guide doesn't cover a question, it says so. Conversations are multi-turn.

**Stack:** LangGraph · Streamlit · Qdrant Cloud · OpenRouter · Langfuse (optional) · one Docker container.

## Run it

1. `git clone https://github.com/PaulaAdelKamal/Paula_henkel_rag_task.git && cd Paula_henkel_rag_task`
2. `cp .env.example .env`, then fill in `OPENROUTER_API_KEY`, `QDRANT_URL` and `QDRANT_API_KEY`. Write values without quotes.
3. `docker build -t chatbot:1.0 .`
4. `docker run -p 8000:8000 --env-file .env chatbot:1.0`
5. Open **http://localhost:8000**

- **Port:** 8000 (change it with `APP_PORT` in `.env` and the same `-p` mapping).
- **No extra steps:** the index is pre-built in Qdrant Cloud, so there's no ingestion step. The app is ready when the container is healthy.
- **If the browser can't connect** while Docker shows the container as healthy, the port wasn't published. Start it with the exact command in step 4, including `-p 8000:8000 --env-file .env`.

## Environment variables

Every variable is in `.env.example`, with a one-line description.

| Variable | Required | Default |
|---|---|---|
| `OPENROUTER_API_KEY` | yes | — |
| `QDRANT_URL` | yes | — |
| `QDRANT_API_KEY` | yes | — |
| `CHAT_MODEL` | no | `google/gemma-4-31b-it:free` |
| `CHAT_FALLBACK_MODELS` | no | `nvidia/nemotron-3.5-super-vl:free,google/gemma-4-26b-a4b-it:free` |
| `EMBEDDING_MODEL` | no | `nvidia/nemotron-3.5-embed-1b:free` |
| `QDRANT_COLLECTION`, `CHUNK_CONFIG_HASH` | no | derived / `edb94b731b` |
| `TOP_K`, `RELEVANCE_THRESHOLD` | no | `5`, `0.0` |
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_BASE_URL` | no | tracing off when empty |
| `APP_ENV`, `APP_PORT`, `LOG_LEVEL`, `CACHE_DIR` | no | `dev`, `8000`, `INFO`, `data/cache` |

## Models

| Role | Model (OpenRouter) | Version / notes |
|---|---|---|
| Answering | `google/gemma-4-31b-it:free` | Gemma 4, 31B instruct; temperature 0 |
| Answering fallbacks | `nvidia/nemotron-3.5-super-vl:free`, then `google/gemma-4-26b-a4b-it:free` | Used automatically if the first model is rate-limited |
| Embeddings | `nvidia/nemotron-3.5-embed-1b:free` | Nemotron 3.5 Embed 1B, 2048 dimensions, `query: ` / `passage: ` prefixes |

The models are free OpenRouter models, checked on 2026-11-01. A key without credit allows 50 free requests per day; when that runs out, the app says so.

The full project README (architecture, chunking, metadata, evaluation, limitations, future work) is in [`README_FULL.md`](README_FULL.md). A description of every file is in [`file_inventory.csv`](file_inventory.csv).
