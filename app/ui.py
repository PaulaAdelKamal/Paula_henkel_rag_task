"""Streamlit chat UI.

Run: streamlit run app/ui.py --server.address 0.0.0.0 --server.port 8000

- Startup self-check first; a failing required service shows a readable error
  screen (no stack trace, no key) instead of a broken chat.
- One LangGraph thread per browser session (memory), one Langfuse trace per turn.
- Every reply carries its **Sources** block in the message text itself, plus an
  expander with the cited passages and a preview of the cited page.
- Progress is streamed node by node; the answer appears in one piece because it
  is produced as a structured (function-call) output, see ADR-0023.
- Thumbs up/down are sent to Langfuse as a score on that turn's trace.
"""

from __future__ import annotations

import logging
import sys
import uuid
from pathlib import Path

import truststore

# `streamlit run app/ui.py` puts app/ (not the repo root) on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
truststore.inject_into_ssl()

import streamlit as st  # noqa: E402

from app.config import ConfigError, get_settings  # noqa: E402
from app.graph import MAX_QUESTION_CHARS, build_rag, collection_for  # noqa: E402
from app.health import startup_checks  # noqa: E402
from app.observability import Tracer, configure_logging  # noqa: E402
from app.pages import page_png  # noqa: E402
from app.prompts import GUIDE  # noqa: E402

STARTERS = [
    "How do I close an app that isn't working?",
    "What does the airplane icon in the status bar mean?",
    "How do I take a screenshot?",
    "How do I set up a passcode?",
]
PROGRESS = {
    "condense": "Understanding your follow-up…",
    "retrieve": "Searching the guide…",
    "generate": "Writing an answer from the guide…",
}

st.set_page_config(page_title="iPhone Guide Assistant", page_icon="📘", layout="centered")


@st.cache_resource(show_spinner="Checking services…")
def load_app():
    """Settings, startup checks, graph and tracer: built once per server process."""
    try:
        settings = get_settings()
    except ConfigError as exc:
        return None, [str(exc)], None, None
    configure_logging(settings.log_level)
    checks = startup_checks(settings)
    blocking = [f"{c.service}: {c.detail}" for c in checks if c.blocking]
    if blocking:
        return settings, blocking, None, None
    return settings, checks, build_rag(settings), Tracer(settings)


def show_startup_error(problems: list[str]) -> None:
    st.title("📘 iPhone Guide Assistant")
    st.error("The assistant can't start because a required service isn't ready.")
    for p in problems:
        st.markdown(f"- {p}")
    st.caption("Check the values in your .env file (see .env.example), then reload this page.")
    st.stop()


def take_starter() -> None:
    """Consume a starter pill once (a selected pill would otherwise re-ask on every rerun)."""
    st.session_state.pending_question = st.session_state.starter
    st.session_state.starter = None


def send_feedback(tracer: Tracer, key: str, trace_id: str | None) -> None:
    value = st.session_state.get(key)
    if value is not None:
        tracer.score_feedback(trace_id, value)


def render_turn(msg: dict, tracer: Tracer | None, index: int) -> None:
    st.markdown(msg["content"])
    used = msg.get("used") or []
    if used:
        with st.expander(f"Cited passages ({len(used)})"):
            for n, chunk in enumerate(used, start=1):
                path = " > ".join(chunk.get("section_path") or []) or "Legal notices"
                pages = chunk.get("pages") or [chunk.get("page")]
                st.markdown(f"**{n}. Page {', '.join(map(str, pages))} · {path}**")
                st.caption(chunk.get("text", "")[:1200])
            first_page = (used[0].get("pages") or [used[0].get("page")])[0]
            if st.toggle(f"Show page {first_page}", key=f"page_{index}"):
                st.image(page_png(first_page), caption=f"Page {first_page} of the guide")
    if tracer is not None and msg.get("decision") in {"answer", "not_in_document"}:
        key = f"fb_{index}"
        st.feedback(
            "thumbs",
            key=key,
            on_change=send_feedback,
            args=(tracer, key, msg.get("trace_id")),
        )


def run_turn(graph, tracer: Tracer, question: str) -> dict:
    session = st.session_state.thread_id
    trace_id = tracer.new_trace_id()
    config = {"configurable": {"thread_id": session}} | tracer.run_config(session)
    with tracer.turn(trace_id, question) as recorder, st.status("Working…") as status:
        for update in graph.stream({"question": question}, config, stream_mode="updates"):
            for node in update:
                if node in PROGRESS:
                    status.update(label=PROGRESS[node])
        state = graph.get_state({"configurable": {"thread_id": session}}).values
        recorder.record(state)
        status.update(label="Done", state="complete")
    tracer.flush()
    return {
        "role": "assistant",
        "content": state.get("response", ""),
        "used": state.get("used", []),
        "decision": state.get("decision"),
        "trace_id": trace_id,
    }


def main() -> None:
    settings, checks, graph, tracer = load_app()
    if graph is None:
        show_startup_error(checks)

    st.session_state.setdefault("thread_id", str(uuid.uuid4()))
    st.session_state.setdefault("messages", [])

    with st.sidebar:
        st.header("About")
        st.markdown(
            f"Answers **only** from the *{GUIDE}* (Apple, 2014). Every answer shows the page "
            "and section it comes from; if the guide doesn't cover a question, the assistant "
            "says so."
        )
        if st.button("New conversation", width="stretch"):
            st.session_state.thread_id = str(uuid.uuid4())
            st.session_state.messages = []
            st.rerun()
        with st.expander("System status"):
            for c in checks:
                st.caption(f"{c.status} · {c.service}: {c.detail}")
            st.caption(f"Collection: {collection_for(settings)}")

    st.title("📘 iPhone Guide Assistant")
    st.caption(f"Ask about the {GUIDE}. Answers come only from that guide, with page references.")

    for i, msg in enumerate(st.session_state.messages):
        with st.chat_message(msg["role"]):
            if msg["role"] == "assistant":
                render_turn(msg, tracer, i)
            else:
                st.markdown(msg["content"])

    if not st.session_state.messages:
        st.pills(
            "Try one of these",
            STARTERS,
            selection_mode="single",
            key="starter",
            on_change=take_starter,
        )
    typed = st.chat_input(
        "Ask a question about the iPhone User Guide", max_chars=MAX_QUESTION_CHARS
    )
    question = typed or st.session_state.pop("pending_question", None)
    if question:
        st.session_state.messages.append({"role": "user", "content": question})
        with st.chat_message("user"):
            st.markdown(question)
        with st.chat_message("assistant"):
            try:
                reply = run_turn(graph, tracer, question)
            except Exception as exc:  # never a stack trace in the chat
                logging.getLogger(__name__).exception("turn failed")
                reply = {
                    "role": "assistant",
                    "content": f"Something went wrong while answering ({type(exc).__name__}). "
                    "Please try again.",
                    "decision": "error",
                }
            st.session_state.messages.append(reply)
            render_turn(reply, tracer, len(st.session_state.messages) - 1)


main()
