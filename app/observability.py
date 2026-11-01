"""Langfuse tracing with graceful degradation.

With keys set, every chat turn becomes one Langfuse trace with a known id (so a
thumbs up/down can be attached later as a score): the LangChain callback
records the LLM calls inside it, and the turn's retrieval results, decision and
answering model are added as metadata. Without keys, or if Langfuse fails in
any way, every method is a no-op: tracing must never break answering.
"""

from __future__ import annotations

import logging
import os
import subprocess
from contextlib import contextmanager, nullcontext
from functools import lru_cache

from app.config import Settings
from app.prompts import PROMPT_VERSION

log = logging.getLogger(__name__)

# Chatty libraries that log every request URL at INFO (e.g. the Qdrant cluster host).
_NOISY_LOGGERS = ("httpx", "httpcore", "urllib3", "openai", "langfuse", "qdrant_client")


def configure_logging(level: str = "INFO") -> None:
    """App logs at LOG_LEVEL; HTTP client libraries only at WARNING unless LOG_LEVEL=DEBUG.

    Keys are never logged by the app (they travel in headers, which these libraries don't
    log at INFO either); user questions go to Langfuse traces only, not to stdout.
    """
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.DEBUG if level == "DEBUG" else logging.WARNING)


@lru_cache(maxsize=1)
def git_sha() -> str:
    """Build-time GIT_SHA (Docker build arg), else the local checkout, else 'unknown'."""
    if sha := os.environ.get("GIT_SHA"):
        return sha[:7]
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv, no user input
            ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


class Tracer:
    """Langfuse tracing for chat turns; every method is a safe no-op when tracing is off."""

    def __init__(self, settings: Settings) -> None:
        """Create the Langfuse client if keys are set; any failure just disables tracing."""
        self.settings = settings
        self.enabled = False
        self._client = None
        if not settings.langfuse_enabled:
            log.info("Langfuse keys not set: tracing disabled")
            return
        try:
            from langfuse import Langfuse

            self._client = Langfuse(
                public_key=settings.langfuse_public_key.get_secret_value(),
                secret_key=settings.langfuse_secret_key.get_secret_value(),
                base_url=settings.langfuse_base_url,
                environment=settings.app_env,
                release=git_sha(),
            )
            self.enabled = True
        except Exception as exc:
            log.warning("Langfuse unavailable, tracing disabled (%s)", type(exc).__name__)

    @property
    def tags(self) -> list[str]:
        """Trace tags: environment, embedding model, chunk config, prompt version, git sha."""
        s = self.settings
        return [
            f"env:{s.app_env}",
            f"embedding:{s.embedding_model}",
            f"chunks:{s.chunk_config_hash}",
            f"prompt:{PROMPT_VERSION}",
            f"git:{git_sha()}",
        ]

    def new_trace_id(self) -> str | None:
        """A fresh trace id for the next turn (None when tracing is off)."""
        if not self.enabled:
            return None
        try:
            from langfuse import Langfuse

            return Langfuse.create_trace_id()
        except Exception:
            return None

    def run_config(self, session_id: str) -> dict:
        """LangChain/LangGraph config: callbacks + trace attributes (empty when disabled)."""
        if not self.enabled:
            return {}
        try:
            from langfuse.langchain import CallbackHandler

            return {
                "callbacks": [CallbackHandler()],
                "metadata": {
                    "langfuse_session_id": session_id,
                    "langfuse_user_id": "anonymous",
                    "langfuse_tags": self.tags,
                },
            }
        except Exception as exc:
            log.warning("Langfuse callback unavailable (%s)", type(exc).__name__)
            return {}

    @contextmanager
    def turn(self, trace_id: str | None, question: str):
        """Root span of one chat turn, created with a known trace id. Yields a recorder."""
        if not self.enabled or trace_id is None:
            yield _NoopRecorder()
            return
        try:
            span_cm = self._client.start_as_current_observation(
                as_type="span",
                name="rag-turn",
                trace_context={"trace_id": trace_id},
                input={"question": question},
            )
        except Exception:
            span_cm = nullcontext(None)
        with span_cm as span:
            yield _SpanRecorder(span)

    def score_feedback(self, trace_id: str | None, value: int, comment: str | None = None) -> None:
        """Thumbs up (1) / down (0) from the UI as a Langfuse score on the turn's trace."""
        if not self.enabled or trace_id is None:
            return
        try:
            self._client.create_score(
                trace_id=trace_id,
                name="user-feedback",
                value=float(value),
                data_type="NUMERIC",
                comment=comment,
            )
        except Exception as exc:
            log.warning("could not send feedback score (%s)", type(exc).__name__)

    def flush(self) -> None:
        """Send buffered spans now (Streamlit sessions are short-lived)."""
        if self.enabled:
            try:
                self._client.flush()
            except Exception as exc:
                log.debug("Langfuse flush failed (%s)", type(exc).__name__)


class _NoopRecorder:
    """Recorder used when tracing is off: records nothing."""

    def record(self, state: dict) -> None:
        """Do nothing."""
        pass


class _SpanRecorder:
    """Writes a finished turn's results onto its Langfuse root span."""

    def __init__(self, span) -> None:
        """Keep the root span of the turn (may be None if Langfuse failed)."""
        self.span = span

    def record(self, state: dict) -> None:
        """Store the response and retrieval metadata on the span; never raises."""
        if self.span is None:
            return
        try:
            retrieved = state.get("retrieved", [])
            self.span.update(
                output={"response": state.get("response", "")},
                metadata={
                    "decision": state.get("decision"),
                    "standalone_question": state.get("standalone_question"),
                    "model_used": state.get("model_used"),
                    "invalid_sources": state.get("invalid_sources"),
                    "error": state.get("error") or None,
                    "retrieved": [
                        {
                            "chunk_id": h.get("chunk_id"),
                            "page": h.get("page"),
                            "score": round(h.get("score", 0.0), 4),
                        }
                        for h in retrieved
                    ],
                    "cited_pages": [c["pages"] for c in state.get("citations", [])],
                },
            )
        except Exception as exc:  # tracing must never break answering
            log.debug("could not record turn metadata (%s)", type(exc).__name__)
