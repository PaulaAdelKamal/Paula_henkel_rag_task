"""The RAG graph with stub retriever / answerer / condenser (no network, no quota)."""

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from app import citations
from app.graph import build_graph
from app.llm import AnswerUnavailable, GroundedAnswer, LLMAnswerer, parse_answer
from app.prompts import EMPTY_QUESTION, NOT_IN_GUIDE, format_sources

MULTITASKING = {
    "chunk_id": "a",
    "page": 22,
    "pages": [22],
    "section_path": ["Chapter 3: Basics", "Use apps", "Multitasking"],
    "text": "Close an app. Drag the app up from the multitasking display.",
    "score": 0.42,
}
STATUS_ICONS = {
    "chunk_id": "b",
    "page": 12,
    "pages": [12, 13],
    "section_path": ["Chapter 1: iPhone at a Glance", "Status icons"],
    "text": "Airplane mode | Airplane mode is on.",
    "score": 0.30,
}


class StubRetriever:
    def __init__(self, hits):
        self.hits, self.queries = hits, []

    def __call__(self, query, k):
        self.queries.append(query)
        return [dict(h) for h in self.hits][:k]


class StubAnswerer:
    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def __call__(self, question, chunks, config=None):
        self.calls.append(question)
        nxt = self.answers.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


class StubCondenser:
    def __init__(self, rewrite=None, fail=False):
        self.rewrite, self.fail, self.calls = rewrite, fail, []

    def __call__(self, history, question, config=None):
        self.calls.append(([m.content for m in history], question))
        if self.fail:
            raise RuntimeError("model down")
        return self.rewrite or question


def run(graph, question, thread="t1"):
    return graph.invoke({"question": question}, {"configurable": {"thread_id": thread}})


def answer(text="Drag the app up.", sources=(1,), answerable=True):
    return GroundedAnswer(answerable=answerable, answer=text, used_sources=list(sources))


# --- answer path ---------------------------------------------------------------------


def test_answer_shows_sources_from_metadata():
    g = build_graph(
        StubRetriever([MULTITASKING, STATUS_ICONS]), StubAnswerer(answer()), StubCondenser()
    )
    out = run(g, "How do I close an app?")
    assert out["decision"] == "answer"
    assert out["response"].startswith("Drag the app up.\n\n**Sources**\n")
    assert "- Page 22 · Chapter 3: Basics > Use apps > Multitasking" in out["response"]
    assert "Page 12" not in out["response"]  # retrieved but not used -> not cited
    assert out["citations"] == [
        {"pages": [22], "section": "Chapter 3: Basics > Use apps > Multitasking"}
    ]


def test_invented_source_numbers_are_dropped():
    g = build_graph(
        StubRetriever([MULTITASKING]), StubAnswerer(answer(sources=(1, 7))), StubCondenser()
    )
    out = run(g, "How do I close an app?")
    assert out["decision"] == "answer" and out["invalid_sources"] == [7]
    assert out["citations"][0]["pages"] == [22]


def test_answerable_without_any_valid_source_becomes_a_refusal():
    g = build_graph(
        StubRetriever([MULTITASKING]), StubAnswerer(answer(sources=(9,))), StubCondenser()
    )
    out = run(g, "How do I close an app?")
    assert out["decision"] == "not_in_document"
    assert out["response"].startswith(NOT_IN_GUIDE)


# --- refusals ------------------------------------------------------------------------------


def test_unanswerable_refusal_lists_searched_sections_as_non_sources():
    g = build_graph(
        StubRetriever([MULTITASKING, STATUS_ICONS]),
        StubAnswerer(answer(text="", sources=(), answerable=False)),
        StubCondenser(),
    )
    out = run(g, "How do I set up Face ID?")
    assert out["decision"] == "not_in_document" and out["citations"] == []
    lines = out["response"].splitlines()
    assert lines[0] == NOT_IN_GUIDE
    assert "**Closest sections searched (not sources of an answer)**" in lines
    # closest sections keep retrieval order (most relevant first), not page order
    assert lines[-2].startswith("- Page 22") and lines[-1].startswith("- Pages 12–13")


def test_relevance_gate_refuses_without_calling_the_model():
    answerer = StubAnswerer()
    g = build_graph(
        StubRetriever([MULTITASKING]), answerer, StubCondenser(), relevance_threshold=0.5
    )
    out = run(g, "What is the price of an iPhone 15?")
    assert out["decision"] == "not_in_document" and answerer.calls == []


def test_empty_retrieval_refuses_without_calling_the_model():
    answerer = StubAnswerer()
    out = run(build_graph(StubRetriever([]), answerer, StubCondenser()), "anything")
    assert out["decision"] == "not_in_document" and answerer.calls == []
    assert out["response"] == NOT_IN_GUIDE  # nothing searched -> no section list


def test_blank_question_costs_nothing():
    retriever = StubRetriever([MULTITASKING])
    out = run(build_graph(retriever, StubAnswerer(), StubCondenser()), "   ")
    assert out["response"] == EMPTY_QUESTION and retriever.queries == []


def test_model_failure_gives_a_clear_message_with_searched_sections():
    g = build_graph(
        StubRetriever([MULTITASKING]),
        StubAnswerer(AnswerUnavailable("no tool call")),
        StubCondenser(),
    )
    out = run(g, "How do I close an app?")
    assert out["decision"] == "error"
    assert "couldn't produce an answer" in out["response"] and "Page 22" in out["response"]


# --- multi-turn memory ------------------------------------------------------------------------


def test_follow_up_is_rewritten_with_history_and_used_for_retrieval():
    retriever = StubRetriever([MULTITASKING])
    condenser = StubCondenser(rewrite="How do I reopen a closed app?")
    g = build_graph(retriever, StubAnswerer(answer(), answer("Tap it.")), condenser)
    run(g, "How do I close an app?")
    assert condenser.calls == []  # first turn: nothing to resolve, no extra request
    out = run(g, "And how do I reopen it?")
    history, question = condenser.calls[0]
    assert history == ["How do I close an app?", "Drag the app up."]
    assert question == "And how do I reopen it?"
    assert retriever.queries[-1] == "How do I reopen a closed app?"
    assert out["standalone_question"] == "How do I reopen a closed app?"


def test_failed_rewrite_falls_back_to_the_raw_question():
    retriever = StubRetriever([MULTITASKING])
    g = build_graph(retriever, StubAnswerer(answer(), answer()), StubCondenser(fail=True))
    run(g, "How do I close an app?")
    run(g, "and then?")
    assert retriever.queries[-1] == "and then?"


def test_sessions_do_not_share_memory():
    condenser = StubCondenser()
    g = build_graph(StubRetriever([MULTITASKING]), StubAnswerer(answer(), answer()), condenser)
    run(g, "How do I close an app?", thread="alice")
    run(g, "And reopen it?", thread="bob")
    assert condenser.calls == []  # bob has no history to resolve against


def test_stale_turn_state_is_reset():
    g = build_graph(
        StubRetriever([MULTITASKING]),
        StubAnswerer(answer(), answer(text="", sources=(), answerable=False)),
        StubCondenser(),
    )
    run(g, "How do I close an app?")
    out = run(g, "What about Face ID?")
    assert out["citations"] == [] and out["used"] == [] and out["answer"] == ""


# --- citations and prompt formatting ----------------------------------------------------------


def test_citation_rendering():
    legal = {"page": 162, "pages": [162], "section_path": [], "text": "IOS is a trademark..."}
    cits = citations.unique_citations([STATUS_ICONS, MULTITASKING, MULTITASKING, legal])
    assert [c.render() for c in cits] == [
        "Pages 12–13 · Chapter 1: iPhone at a Glance > Status icons",
        "Page 22 · Chapter 3: Basics > Use apps > Multitasking",
        "Page 162 · Legal notices (back page)",
    ]


def test_prompt_sources_carry_no_page_numbers():
    text = format_sources([MULTITASKING, STATUS_ICONS])
    assert text.startswith("[1] Chapter 3: Basics > Use apps > Multitasking\n")
    assert "[2] Chapter 1: iPhone at a Glance > Status icons" in text
    assert "22" not in text and "12" not in text


# --- LLM adapter --------------------------------------------------------------------------------


class ToolCallingFake(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def test_answerer_reads_the_tool_call():
    msg = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "GroundedAnswer",
                "args": {"answerable": True, "answer": "Drag it up.", "used_sources": [1]},
                "id": "call-1",
            }
        ],
    )
    result = LLMAnswerer(ToolCallingFake(responses=[msg]))("How?", [MULTITASKING])
    assert result == GroundedAnswer(answerable=True, answer="Drag it up.", used_sources=[1])


def test_parse_answer_falls_back_to_json_in_text_and_rejects_prose():
    raw = AIMessage(content='Sure: {"answerable": false, "answer": "", "used_sources": []}')
    assert parse_answer({"parsed": None, "raw": raw}).answerable is False
    with pytest.raises(AnswerUnavailable):
        parse_answer({"parsed": None, "raw": AIMessage(content="I think you swipe up.")})
