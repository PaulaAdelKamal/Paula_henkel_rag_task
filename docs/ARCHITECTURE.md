# Architecture

> Filled in from Step 5 (graph diagram) onwards. Planned shape:

```
offline:  PDF -> parse_pdf -> figures -> chunking -> ingest (OpenRouter embeddings) -> Qdrant Cloud
runtime:  Streamlit UI -> LangGraph [condense -> retrieve -> relevance gate -> generate -> citations]
          -> OpenRouter (chat + query embedding), Qdrant Cloud (search), Langfuse (traces, optional)
```
