"""The Streamlit app run headlessly with AppTest; services are stubbed (no network, no quota)."""

from contextlib import contextmanager
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

import app.config
import app.graph
import app.health
import app.observability
from app.config import Settings
from app.graph import build_graph
from app.health import Check
from app.llm import GroundedAnswer
from tests.test_graph import MULTITASKING, StubCondenser, StubRetriever

UI = str(Path(__file__).resolve().parent.parent / "app" / "ui.py")


class FakeTracer:
    enabled = True
    scores: list = []
    recorded: list = []

    def __init__(self, settings=None):
        pass

    def new_trace_id(self):
        return "trace-1"

    def run_config(self, session_id):
        return {}

    @contextmanager
    def turn(self, trace_id, question):
        class Recorder:
            def record(self, state):
                FakeTracer.recorded.append((trace_id, state["decision"]))

        yield Recorder()

    def score_feedback(self, trace_id, value, comment=None):
        FakeTracer.scores.append((trace_id, value))

    def flush(self):
        pass


class Answerer:
    """Answers questions about closing apps, refuses everything else."""

    def __call__(self, question, chunks, config=None):
        if "close" in question.lower():
            return GroundedAnswer(answerable=True, answer="Drag the app up.", used_sources=[1])
        return GroundedAnswer(answerable=False, answer="", used_sources=[])


def settings(**overrides):
    values = {
        "openrouter_api_key": "sk-test",  # pragma: allowlist secret
        "qdrant_url": "http://q",
        "qdrant_api_key": "k",  # pragma: allowlist secret
    }
    return Settings(_env_file=None, **(values | overrides))


@pytest.fixture
def patched(monkeypatch):
    st.cache_resource.clear()
    FakeTracer.scores, FakeTracer.recorded = [], []
    monkeypatch.setattr(app.config, "get_settings", lambda: settings())
    monkeypatch.setattr(app.health, "startup_checks", lambda s: [Check("OpenRouter", "OK", "fine")])
    monkeypatch.setattr(
        app.graph,
        "build_rag",
        lambda s: build_graph(StubRetriever([MULTITASKING]), Answerer(), StubCondenser()),
    )
    monkeypatch.setattr(app.observability, "Tracer", FakeTracer)
    yield monkeypatch
    st.cache_resource.clear()


def ask(at: AppTest, question: str) -> AppTest:
    at.chat_input[0].set_value(question).run()
    return at


def assistant_text(at: AppTest) -> str:
    return "\n".join(m.value for m in at.chat_message[-1].markdown)


def test_startup_failure_shows_a_readable_error(patched):
    patched.setattr(
        app.health,
        "startup_checks",
        lambda s: [Check("Qdrant", "FAIL", "collection iphone_guide__x does not exist")],
    )
    at = AppTest.from_file(UI, default_timeout=30).run()
    assert at.error and "required service" in at.error[0].value
    assert any("does not exist" in m.value for m in at.markdown)
    assert not at.chat_input  # no chat until the service is fixed
    assert not at.exception


def test_reload_after_a_transient_startup_failure_recovers(patched):
    results = iter(
        [[Check("OpenRouter", "FAIL", "ConnectError: certificate verify failed")]],
    )
    patched.setattr(
        app.health, "startup_checks", lambda s: next(results, [Check("OpenRouter", "OK", "ok")])
    )
    first = AppTest.from_file(UI, default_timeout=30).run()
    assert first.error and not first.chat_input
    reloaded = AppTest.from_file(UI, default_timeout=30).run()  # a browser reload = new session
    assert not reloaded.error and reloaded.chat_input
    assert not reloaded.exception


def test_answer_shows_sources_passages_and_page_preview(patched):
    at = ask(AppTest.from_file(UI, default_timeout=30).run(), "How do I close an app?")
    text = assistant_text(at)
    assert "Drag the app up." in text and "**Sources**" in text
    assert "Page 22 · Chapter 3: Basics > Use apps > Multitasking" in text
    assert "Cited passages (1)" in [e.label for e in at.expander]
    at.toggle(key="page_1").set_value(True).run()
    assert at.image  # the cited page is rendered from the bundled PDF
    assert FakeTracer.recorded == [("trace-1", "answer")]
    assert not at.exception


def test_refusal_lists_closest_sections(patched):
    at = ask(AppTest.from_file(UI, default_timeout=30).run(), "How do I set up Face ID?")
    text = assistant_text(at)
    assert "couldn't find the answer" in text
    assert "Closest sections searched (not sources of an answer)" in text


def test_feedback_is_sent_as_a_score(patched):
    at = ask(AppTest.from_file(UI, default_timeout=30).run(), "How do I close an app?")
    at.feedback(key="fb_1").set_value(1).run()
    assert FakeTracer.scores == [("trace-1", 1)]


def test_starter_question_is_asked_once(patched):
    at = AppTest.from_file(UI, default_timeout=30).run()
    at.pills(key="starter").set_value("How do I close an app that isn't working?").run()
    assert len(at.session_state["messages"]) == 2
    at.run()  # an unrelated rerun must not ask it again
    assert len(at.session_state["messages"]) == 2


def test_answers_without_langfuse(patched):
    patched.setattr(app.observability, "Tracer", app.observability.Tracer)  # the real one
    at = ask(AppTest.from_file(UI, default_timeout=30).run(), "How do I close an app?")
    assert "**Sources**" in assistant_text(at) and not at.exception


def test_ten_turn_conversation_with_follow_ups(patched):
    condenser = StubCondenser(rewrite="How do I close an app?")
    patched.setattr(
        app.graph,
        "build_rag",
        lambda s: build_graph(StubRetriever([MULTITASKING]), Answerer(), condenser),
    )
    at = AppTest.from_file(UI, default_timeout=30).run()
    questions = (
        ["How do I close an app?"] + ["And what about it?", "How?"] * 4 + ["Thanks, and it?"]
    )
    for q in questions:
        ask(at, q)
        assert "Page 22" in assistant_text(at), q  # every reply carries page info
    assert len(at.session_state["messages"]) == 20
    assert len(condenser.calls) == 9  # every turn after the first is rewritten with history
    history, _ = condenser.calls[-1]
    assert len(history) == 6  # only the last 3 turns are sent
    assert not at.exception
