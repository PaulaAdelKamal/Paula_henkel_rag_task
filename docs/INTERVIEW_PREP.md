# Interview prep

One section per step: likely questions and crisp answers, including trade-offs and what was rejected.

---

## Step 0: what I must be able to explain

**1. How do you guarantee that no API key ever reaches the repo or the image?**
Four layers, starting before the first line of code:
- `.env` is listed in `.gitignore` *and* `.dockerignore` in the very first commit.
- Keys are `SecretStr` in config, so they are masked in logs and reprs, and config errors name the variable, never its value.
- Two secret scanners run on every commit (detect-secrets with a baseline, gitleaks on staged changes). gitleaks also scans the full history on push and in CI.
- `.env.example` holds only placeholders.

If a key ever leaks, the order is: rotate the key first, then clean history. Once a key is pushed it must be treated as compromised, even after a history rewrite.

**2. Why two secret scanners?**
They detect differently. detect-secrets uses entropy plus plugins, with a reviewed baseline. gitleaks uses a large rule set of provider-specific regexes. The cost is tiny, and a miss is expensive. gitleaks in CI also covers anyone who commits without the hooks installed.

**3. Why uv and dependency groups?**
- `uv.lock` pins every transitive package with hashes, so the build is reproducible.
- Groups keep the runtime image slim: only `[project.dependencies]` ships. `dev`, `ingest` and later `eval` (ragas, PyMuPDF, pytest) never enter the container.
- That also keeps PyMuPDF's AGPL licence out of the distributed image.

**4. Why Streamlit when the brief prefers Chainlit?**
Chainlit 2.12, the latest, declares `Requires-Python <3.14`. Our interpreter is 3.14, and on this machine no other interpreter can run (app control). Running Chainlit outside its declared support range would be a hidden risk.

What changes with Streamlit:
- The script re-runs on every interaction, so session state is explicit (`st.session_state`, plus a LangGraph checkpointer keyed by a per-session `thread_id`).
- Feedback uses `st.feedback`.

The mandatory requirements (citations, refusals, multi-turn, single container) don't depend on the UI framework.

**5. Why Qdrant Cloud over Pinecone?**
The benchmark needs one index per embedding model, and dimensions differ (384 to 4096). Qdrant's free cluster holds many collections with different dimensions, plus payload indexes and sparse vectors for hybrid search. Pinecone's free tier limits the number of indexes, and each index has one dimension.

The risk to manage is the free-tier inactivity policy: suspended after 1 week idle, deleted after 4 weeks. Mitigation: a keep-alive / verify script, and a re-check before submission and before the interview.

**6. Why route every model through OpenRouter?**
- One key, one bill, one OpenAI-compatible API for chat, embeddings, vision and the judge.
- The listing endpoints let us discover and record exact model slugs instead of typing them from memory.
- `GET /key` validates a key at zero cost.

Trade-off: a single point of failure, and per-model behaviour (e.g. embedding prefixes) is our responsibility.

**7. Why `LANGFUSE_BASE_URL` and not `LANGFUSE_HOST` as in the brief?**
The current Langfuse Python SDK (v5) reads `LANGFUSE_BASE_URL` and marks `LANGFUSE_HOST` as deprecated. Following the SDK means its auto-configuration works without glue code. Tracing is optional: missing keys disable it, they don't crash the app.

**8. What does `scripts/check_connections.py` actually verify, and why doesn't it spend money?**
- OpenRouter: `GET /key` (key validity, remaining limit), and that the configured chat and embedding slugs exist in the live listings.
- Qdrant: `get_collections()`.
- Langfuse: `auth_check()`, reported as SKIP if no keys are set.

None of these calls is billable. Every failure prints as one line, with secret values redacted.
