# Evaluation and optimisation: future work

Steps 9 (evaluation harness and embedding benchmark) and 10 (optimisation ablations) of the plan were **not executed** in this submission. The project ran on free OpenRouter models with a 50-requests-per-day cap, which is far too little for a fair multi-model comparison. This page records what would be measured, how, and with which assets already in place. The decision is ADR-0027.

Nothing below is a measured result. The current system's evidence is the manual gate checks (8/8 in Step 5, the in-container smoke test in Step 7) and the reviewed golden set (Step 8).

## 1. What is already in place

| Asset | Where | Ready for |
|---|---|---|
| Golden set: 80 human-reviewed questions, 9 categories, gold pages derived from verified evidence quotes | `eval/datasets/golden.jsonl`, Langfuse dataset `golden` | every metric below |
| 14 chunk configurations (structure-aware 200/350/500 × overlap × header, figure labels on/off, naive baseline) | `python -m ingestion.chunking --grid` | chunking ablations |
| Embedding registry with per-model prefixes; idempotent per-model collections | `app/embeddings.py`, `ingestion/ingest.py` | embedding benchmark |
| Two ingested embedding collections (Nemotron 3.5 1B, LFM 2.5 350M) | Qdrant Cloud | first benchmark run |
| Answering model and fallbacks selected by environment variables | `CHAT_MODEL`, `CHAT_FALLBACK_MODELS` | answering-model comparison |
| One trace per turn with retrieved chunk ids, scores, decision and answering model | Langfuse | experiment runs and failure analysis |

## 2. Planned comparisons

### 2.1 Embedding models (retrieval only, cheap)

Embed the 80 golden questions with each model's query prefix (batched, a few requests per model) and score retrieval at **page level** against the gold pages:

- hit@1/3/5/10, recall@k, MRR, nDCG@k, plus section-level hit;
- broken down by category: figure, cross-reference and model near-miss questions behave differently from single facts;
- **95% bootstrap confidence intervals.** With 63 answerable questions, differences of a few points are within noise and must be reported as such.

Candidates:
- the free models already in the registry (Nemotron 3.5 1B and 8B, Nemotron 3 1B, Llama Nemotron Embed VL 1B, LFM 2.5 350M);
- paid open-weight models: **Qwen3-Embedding 4B and 8B**, BGE-M3, E5-large-v2, GTE-large;
- optionally one proprietary baseline (OpenAI text-embedding-3-small).

For each model, record dimension, latency per batch, price per 1M tokens and index size, because "best" is multi-objective.

### 2.2 Answering models (end to end)

Run the full graph over the golden set for each answering model and measure:

- **Refusal precision and recall:** answerable vs unanswerable, including the hard negatives (Apple Pay vs Passbook, battery health vs battery percentage, StandBy vs "standby mode").
- **Citation accuracy** (cited pages intersect the gold pages) and **citation validity** (cited pages are among the retrieved ones).
- **Adversarial behaviour:** injection, the false "cite page 12" premise, prompt extraction.
- **Ragas metrics:** faithfulness, response relevancy, context precision and recall, factual correctness. Use a judge from a **different model family** than the answering model, at temperature 0.
- Latency p50/p95 and cost per question.

Candidates:
- the current free chain (Gemma 4 31B → Nemotron 3.5 Super VL → Gemma 4 26B);
- **GPT-6 Luna** and **Claude Haiku 5.5**;
- one larger model as a ceiling.

Each configuration becomes a Langfuse experiment run named `emb=<slug>|chunk=<hash>|chat=<slug>|git=<sha>` over the `golden` dataset, so runs can be compared item by item in the UI.

### 2.3 Ablations (one factor at a time, stop when gains are within noise)

1. Chunk size, overlap and contextual header, using the best embedding model (all 14 configurations already exist).
2. Figure callout labels in the index: on vs off, judged mainly on the `figure` category.
3. Retrieval: top-k 3/5/8, MMR, dense vs hybrid (dense + BM25/sparse vectors in Qdrant).
4. Relevance-gate threshold, **calibrated per embedding model** from the score distributions of answerable vs unanswerable questions. The Step 5 data point (Face ID 0.483 > screenshot 0.318) shows that one global cutoff cannot work.
5. Query rewriting on/off, measured on the multi-turn category.
6. Following "See … on page N" cross-references at retrieval time, measured on the cross-reference category.

## 3. Scaling to millions of documents

The current design deliberately fits one 162-page guide (336 chunks). For a corpus of millions of documents, the plan would add:

- **A reranker:** a cross-encoder or hosted rerank model over the top 50–100 dense hits. This is the single biggest quality gain once the first-stage candidate list gets noisy.
- **Hybrid retrieval:** dense plus sparse (BM25/SPLADE) fused with reciprocal rank fusion, for exact terms such as model names, menu paths and error codes.
- **Metadata filtering and routing:** filter on product, version and language before vector search, or route queries to per-product collections.
- **Query understanding:** decomposition for multi-part questions, and HyDE or multi-query expansion when recall drops.
- **Index engineering:** quantisation (scalar/binary), HNSW tuning, sharding and replication, and incremental ingestion with deduplication by content hash.
- **Caching:** query-embedding and answer caches for frequent questions.
- **Evaluation at scale:** a larger synthetic set (the Ragas test-set generator, kept apart from the golden set) and online feedback from the thumbs up/down scores already sent to Langfuse.

None of these is needed at the current scale. A 336-chunk index is small enough that a good embedding model, structure-aware chunking and a strict grounding prompt give precise answers.

## 4. Preferred production combination (owner's choice, not yet benchmarked)

| Role | Model | Price (OpenRouter, 2026-11-01) | Why |
|---|---|---|---|
| Embeddings | **Qwen3-Embedding 4B** (`qwen/qwen3-embedding-4b`, hosted) | $0.02 per 1M tokens | Strong open-weight retrieval model, 32k context, hosted (no model download in the container) |
| Answering | **GPT-6 Luna** (`openai/gpt-6-luna`) or **Claude Haiku 5.5** (`anthropic/claude-haiku-5.5`) | $0.10 in / $0.50 out per 1M tokens | Low price point; both support strict structured outputs, so the `{answerable, answer, used_sources}` contract can use JSON-schema mode instead of the function-calling workaround needed for the free Gemma model |

**Why small models are enough here.** The task isn't demanding: each answer is a short, grounded synthesis of about five retrieved passages, with citations rendered by code, not by the model.
- A typical question costs about 1,400 input and 100 output tokens: **about $0.0002 per question, about $0.20 per 1,000 questions**.
- Indexing the whole guide costs about 64k tokens, **about $0.001, once**.

**Open points the benchmark would settle.**
- On the same listing, Qwen3-Embedding 8B is priced lower ($0.01 per 1M) than the 4B.
- Qwen3 embeddings need a query-side instruction prefix, to be added to the registry from the model card.
- GPT-6 Luna vs Claude Haiku 5.5 should be chosen on refusal and citation accuracy, not price, since they cost the same.

**Switching** is configuration plus one ingestion run, no code change in the graph:
1. Add an `EmbeddingSpec` for the new embedding model and run `python -m ingestion.ingest --model qwen/qwen3-embedding-4b --yes`.
2. Set `EMBEDDING_MODEL`, `CHAT_MODEL` and `CHAT_FALLBACK_MODELS` in `.env`. The free-only filter on fallbacks would be relaxed deliberately.
