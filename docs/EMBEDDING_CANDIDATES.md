# Embedding candidates (Step 4)

Project constraint (set by the project owner): **free OpenRouter models only**. On 2026-11-01, `GET /api/v1/embeddings/models` listed 38 embedding models, five of them free (`:free`). All five are in the registry (`app/embeddings.py`). Three were shortlisted for ingestion, to fit the key's free-tier quota of 50 free-model requests per day.

## Registry

| Model (OpenRouter slug) | Dim | Query / document prefix | Max tokens | Normalised | License | Status |
|---|---|---|---|---|---|---|
| `nvidia/nemotron-3.5-embed-1b:free` | 2048 | `query: ` / `passage: ` | 32,768 | yes (probe: norm 1.0) | OpenMDW-1.1 | **ingested** (runtime default) |
| `liquid/lfm-2.5-embedding-350m:free` | 1024 | `query: ` / **`document: `** | 512 | yes (probe: norm 1.0) | LFM Open License v1.0 | **ingested** |
| `nvidia/nemotron-3.5-embed-8b:free` | 4096 | `query: ` / `passage: ` | 32,768 | yes (per card) | OpenMDW-1.1 | **shortlisted, not ingested**: the hosted endpoint fails on batched requests (see below) |
| `nvidia/nemotron-3-embed-1b:free` | 2048 | `query: ` / `passage: ` | 32,768 | yes (per card) | OpenMDW-1.1 | registry only (quota) |
| `nvidia/llama-nemotron-embed-vl-1b-v2:free` | 2048 | `query: ` / `passage: ` | 8,192 (text, per card) | not stated | NVIDIA Open Model License + Llama 3.2 | registry only (quota) |

**Sources:** each model's Hugging Face card and `config_sentence_transformers.json` (`nvidia/Nemotron-3.5-Embed-1B-BF16`, `nvidia/Nemotron-3.5-Embed-8B-BF16`, `nvidia/Nemotron-3-Embed-1B-BF16`, `nvidia/llama-nemotron-embed-vl-1b-v2`, `LiquidAI/LFM2.5-Embedding-350M`), and OpenRouter's model listing.

**Why the prefixes matter.** These models were trained with different texts on the query side and the document side. Without the right prefix, queries and documents are embedded inconsistently and recall drops silently, with no error. LFM is the trap: its document prefix is `document: `, not the `passage: ` the others use.

## Probe results (one request per model, `scripts/probe_embeddings.py`)

The model cards can't say how OpenRouter's hosted endpoints behave, so each model was probed with one batched request: the raw query, the same query with `query: `, and the longest chunk.

| Model | Dim returned | Norm | cos(raw, prefixed query) | API tokens vs BERT estimate |
|---|---|---|---|---|
| Nemotron 3.5 1B | 2048 ✓ | 1.000 | 0.941 | 373 vs 357 (+4.5%) |
| LFM 2.5 350M | 1024 ✓ | 1.000 | 0.979 | 368 vs 357 (+3.1%) |
| Nemotron 3.5 8B | 4096 ✓ (single input) | – | – | batched probe: HTTP 502 "service inference failed" |

- **Prefixes aren't added upstream.** Adding `query: ` changes the vector (cosine < 1), so the prefix reaches the model, and the client must add it, as it does.
- **Vectors come back normalised.** The client re-normalises anyway, and Qdrant uses cosine distance.
- **The BERT token estimate runs 3–5% low.** At ingestion, API usage was 67,317 tokens (Nemotron) and 66,755 (LFM) against 64,001 estimated. The longest chunk (339 BERT tokens, about 355 real) is still far under LFM's 512-token limit.

**Nemotron 3.5 8B.** Single inputs work, including the longest chunk on its own (dim 4096). Batched requests fail with a 502 from the provider, both at 3 inputs (the probe) and at 16 (ingestion with `--batch-size 16 --max-retries 1`). Ingesting it would take at least one request per few chunks: 42+ requests at 8 inputs per request, more than a day's quota. It's deferred. The empty collection created by the failed run was deleted.

## Ingestion (`ingestion/ingest.py`, chunk config `edb94b731b`, 336 chunks)

| Model | Collection | Points | Requests | API tokens | Duration | Cost |
|---|---|---|---|---|---|---|
| Nemotron 3.5 1B | `iphone_guide__nvidia_nemotron_3_5_embed_1b_free__edb94b731b` | 336 | 6 | 67,317 | 32.9 s | $0 |
| LFM 2.5 350M | `iphone_guide__liquid_lfm_2_5_embedding_350m_free__edb94b731b` | 336 | 6 | 66,755 | 23.8 s | $0 |

Reports: `data/reports/ingestion_*.json`, `data/reports/embedding_probe.json`.

**Gate (`ingestion/verify_index.py`):**
- Point count = chunk count (336) and the dimension matches the model, for both collections.
- "How do I close an app?" returns p. 22 (Basics > Use apps > Multitasking) at **rank 1** for both models.
- Re-running ingestion made **0 requests** and changed nothing: ids are deterministic and only missing or stale points are synced.

Qdrant storage: 336 × (2048 + 1024) float32 ≈ 4 MB, far below the free cluster's 1 GB RAM.

## Quota and policy notes

- **Free tier:** 50 free-model requests per day (1000 per day once $10 of credit has been bought, and the models stay free), plus 20 requests per minute. Embedding requests count against the daily cap, shared with the free chat model. Step 4 used 16 of 50.
- **Data policy:** free endpoints may retain prompts. The LFM listing says requests "may be retained and used to train". The source is a public Apple manual, so that's acceptable here; it would not be for private documents.
- **Availability:** free models can be rate-limited or withdrawn. The startup self-check (Step 6) must report a missing or failing embedding model clearly. The registry makes switching a one-variable change, as long as that model's collection is ingested.
