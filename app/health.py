"""Startup self-check: is everything the chatbot needs reachable and consistent?

Used by the UI before the first message and by scripts/check_connections.py.
Each check returns one human-readable line; secret values are redacted from
every message. Required checks (OpenRouter, Qdrant collection) block the UI
with a clear error screen; Langfuse is optional and only warns.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import httpx

from app import vectorstore
from app.config import OPENROUTER_BASE_URL, Settings
from app.embeddings import get_spec

TIMEOUT_S = 15.0


@dataclass
class Check:
    """Result of one startup check: service, status (OK/FAIL/WARN/SKIP) and a readable detail."""

    service: str
    status: str  # OK | FAIL | WARN | SKIP
    detail: str
    required: bool = True

    @property
    def blocking(self) -> bool:
        """A failed required check blocks the chat; optional ones never do."""
        return self.required and self.status == "FAIL"


def redact(text: str, settings: Settings) -> str:
    """Remove every secret value from a message before it is shown or logged."""
    for secret in (
        settings.openrouter_api_key,
        settings.qdrant_api_key,
        settings.langfuse_public_key,
        settings.langfuse_secret_key,
    ):
        value = secret.get_secret_value() if secret else ""
        if value:
            text = text.replace(value, "***")
    return text


def check_openrouter(s: Settings) -> Check:
    """Validate the key for free (GET /key) and that the configured models are listed."""
    headers = {"Authorization": f"Bearer {s.openrouter_api_key.get_secret_value()}"}
    with httpx.Client(base_url=OPENROUTER_BASE_URL, headers=headers, timeout=TIMEOUT_S) as client:
        resp = client.get("/key")  # validates the key without spending credit
        if resp.status_code != 200:
            return Check("OpenRouter", "FAIL", f"API key rejected (HTTP {resp.status_code})")
        info = resp.json()["data"]
        chat_ids = {m["id"] for m in client.get("/models").json()["data"]}
        emb_ids = {m["id"] for m in client.get("/embeddings/models").json()["data"]}
    missing = [m for m in s.chat_models if m not in chat_ids]
    if s.chat_model in missing:
        return Check("OpenRouter", "FAIL", f"CHAT_MODEL '{s.chat_model}' is not available")
    if s.embedding_model not in emb_ids:
        return Check(
            "OpenRouter", "FAIL", f"EMBEDDING_MODEL '{s.embedding_model}' is not available"
        )
    quota = info.get("free_model_daily_requests") or {}
    parts = [f"key valid; chat={s.chat_model}", f"embedding={s.embedding_model}"]
    if quota:
        parts.append(f"free requests left today: {quota.get('remaining')}/{quota.get('limit')}")
    if missing:
        parts.append(f"unavailable fallbacks ignored: {', '.join(missing)}")
    return Check("OpenRouter", "OK", "; ".join(parts))


def check_qdrant(s: Settings) -> Check:
    """Cluster reachable, collection present and non-empty, dimension = embedding model's."""
    from app.graph import collection_for

    client = vectorstore.make_client(s.qdrant_url, s.qdrant_api_key.get_secret_value())
    try:
        st = vectorstore.status(client, collection_for(s))
    finally:
        client.close()
    problems = st.problems(expected_dim=get_spec(s.embedding_model).dim)
    if problems:
        return Check("Qdrant", "FAIL", "; ".join(problems))
    return Check("Qdrant", "OK", f"collection {st.name}: {st.points} chunks, dim {st.dim}")


def check_langfuse(s: Settings) -> Check:
    """Validate Langfuse keys with a plain REST call.

    Deliberately not via the SDK: Langfuse v5 shares one client per public key in
    the process, so creating and shutting down a client here would also shut down
    the tracer's client (traces and feedback scores would then be dropped).
    """
    if not s.langfuse_enabled:
        return Check("Langfuse", "SKIP", "keys not set; tracing disabled (optional)", False)
    resp = httpx.get(
        f"{s.langfuse_base_url.rstrip('/')}/api/public/projects",
        auth=(s.langfuse_public_key.get_secret_value(), s.langfuse_secret_key.get_secret_value()),
        timeout=TIMEOUT_S,
    )
    if resp.status_code != 200:
        return Check(
            "Langfuse",
            "WARN",
            f"credentials rejected by {s.langfuse_base_url} (HTTP {resp.status_code})",
            False,
        )
    return Check("Langfuse", "OK", f"tracing to {s.langfuse_base_url}", False)


def run_check(name: str, fn: Callable[[Settings], Check], s: Settings, required: bool = True):
    """Run one check; any exception becomes a single redacted FAIL/WARN line."""
    try:
        return fn(s)
    except Exception as exc:  # one line per failure, never a stack trace or a key
        status = "FAIL" if required else "WARN"
        return Check(name, status, redact(f"{type(exc).__name__}: {exc}", s), required)


def startup_checks(s: Settings) -> list[Check]:
    """All startup checks in display order (OpenRouter, Qdrant required; Langfuse optional)."""
    return [
        run_check("OpenRouter", check_openrouter, s),
        run_check("Qdrant", check_qdrant, s),
        run_check("Langfuse", check_langfuse, s, required=False),
    ]
