"""The RAG pipeline as a LangGraph state machine.

    START -> prepare --(blank)--> respond_empty -> END
               |
               v
            condense -> retrieve --(no hit above threshold)--> not_in_document -> END
                          |
                          v
                       generate --(not answerable / no valid source)--> not_in_document
                          |   \\--(model error)--> respond_error -> END
                          v
                       respond_answer -> END

Budget per turn: 1 query embedding + 1 chat request, plus 1 chat request to
rewrite a follow-up when there is history. The relevance gate can end a turn
before any chat request.

Grounding guarantees live in code, not in the prompt alone:
- the model returns source *numbers*; numbers that do not exist are dropped;
- "answerable" with no valid source becomes a refusal;
- citations (pages, sections) are rendered from chunk metadata.
Memory: a LangGraph checkpointer keyed by thread_id (one per chat session).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Annotated, Any, Protocol, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from app import citations, vectorstore
from app.config import Settings
from app.embeddings import EmbeddingCache, OpenRouterEmbeddings, get_spec
from app.llm import GroundedAnswer, LLMAnswerer, LLMCondenser, make_chat_model
from app.prompts import (
    ANSWER_FAILED,
    EMPTY_QUESTION,
    NOT_IN_GUIDE,
    RATE_LIMITED,
    SERVICE_UNAVAILABLE,
)

HISTORY_MESSAGES = 6  # last 3 turns are enough to resolve "it" / "that"
MAX_QUESTION_CHARS = 2000


class Retriever(Protocol):
    """Anything that turns a query into ranked chunk payloads (real: Qdrant; tests: stubs)."""

    def __call__(self, query: str, k: int) -> list[dict]:
        """Return up to k chunk payloads with a 'score', best first."""
        ...


Answerer = Callable[..., GroundedAnswer]  # (question, chunks, config=None)
Condenser = Callable[..., str]  # (history, question, config=None)


class RAGState(TypedDict, total=False):
    """The graph's state: persisted conversation plus this turn's working fields."""

    messages: Annotated[list[AnyMessage], add_messages]  # persisted conversation
    question: str  # this turn's raw user message
    standalone_question: str
    retrieved: list[dict]  # chunk payloads + "score", in rank order
    used: list[dict]  # validated chunks the answer is based on
    invalid_sources: list[int]  # source numbers the model invented
    decision: str  # answer | not_in_document | empty | error
    error: str  # exception type + message when a step failed (never shown to the user)
    error_kind: str  # rate_limit | unavailable | no_answer (picks the user-facing message)
    model_used: str  # chat model that produced the answer (may be a fallback)
    answer: str
    citations: list[dict]  # [{"pages": [...], "section": "..."}]
    response: str  # what the user sees (answer or refusal + citations)


# --------------------------------------------------------------------------- nodes


def _prepare(state: RAGState) -> dict:
    """Reset per-turn fields (the checkpoint keeps the previous turn's values)."""
    question = (state.get("question") or "").strip()[:MAX_QUESTION_CHARS]
    return {
        "question": question,
        "standalone_question": question,
        "retrieved": [],
        "used": [],
        "invalid_sources": [],
        "decision": "",
        "error": "",
        "error_kind": "",
        "model_used": "",
        "answer": "",
        "citations": [],
        "response": "",
    }


def _route_prepare(state: RAGState) -> str:
    """A blank question ends the turn immediately; anything else goes to condense."""
    return "respond_empty" if not state["question"] else "condense"


def _make_condense(condenser: Condenser):
    """Build the condense node around a condenser function."""

    def condense(state: RAGState, config: RunnableConfig) -> dict:
        """Rewrite a follow-up into a standalone question from the last turns.

        Does nothing on the first turn; a failed rewrite keeps the raw question.
        """
        history: Sequence[BaseMessage] = state.get("messages", [])[-HISTORY_MESSAGES:]
        if not history:
            return {}
        try:
            return {"standalone_question": condenser(history, state["question"], config=config)}
        except Exception:  # a failed rewrite must not fail the turn: use the raw question
            return {}

    return condense


def classify_error(exc: Exception) -> str:
    """Map an exception to the message family the user sees.

    rate_limit:  free-model quota exhausted or provider 429 (after the fallback chain)
    no_answer:   the model answered, but not in a verifiable structured form
    unavailable: anything else (network, Qdrant, provider outage)
    """
    from app.embeddings import QuotaExhausted
    from app.llm import AnswerUnavailable

    name = type(exc).__name__
    text = str(exc).lower()
    if isinstance(exc, QuotaExhausted) or "ratelimit" in name.lower() or "429" in text:
        return "rate_limit"
    if isinstance(exc, AnswerUnavailable):
        return "no_answer"
    return "unavailable"


def _error_update(exc: Exception) -> dict:
    """State update for a failed step: decision 'error' plus classified detail."""
    return {
        "decision": "error",
        "error": f"{type(exc).__name__}: {str(exc)[:300]}",
        "error_kind": classify_error(exc),
    }


def _make_retrieve(retriever: Retriever, k: int):
    """Build the retrieve node around a retriever."""

    def retrieve(state: RAGState) -> dict:
        """Fetch the top-k chunks for the standalone question; failures become an error state."""
        try:
            return {"retrieved": retriever(state["standalone_question"], k)}
        except Exception as exc:  # quota, network or Qdrant failure: a clear message, no crash
            return _error_update(exc)

    return retrieve


def _make_route_retrieve(threshold: float):
    """Build the router that runs after retrieval (relevance gate)."""

    def route(state: RAGState) -> str:
        """Errors -> respond_error; nothing relevant -> not_in_document; otherwise generate."""
        if state.get("decision") == "error":
            return "respond_error"
        hits = state["retrieved"]
        if not hits or hits[0].get("score", 1.0) < threshold:
            return "not_in_document"
        return "generate"

    return route


def _make_generate(answerer: Answerer):
    """Build the generate node around an answerer."""

    def generate(state: RAGState, config: RunnableConfig) -> dict:
        """Ask for a structured answer, keep only real source numbers, refuse if none remain."""
        chunks = state["retrieved"]
        try:
            result = answerer(state["standalone_question"], chunks, config=config)
        except Exception as exc:  # reported to the user as a clear message, detail kept in state
            return _error_update(exc)
        valid = [n for n in dict.fromkeys(result.used_sources) if 1 <= n <= len(chunks)]
        invalid = [n for n in result.used_sources if n not in valid]
        used = [chunks[n - 1] for n in valid]
        ok = result.answerable and bool(used) and bool(result.answer.strip())
        return {
            "decision": "answer" if ok else "not_in_document",
            "answer": result.answer.strip() if ok else "",
            "used": used,
            "invalid_sources": invalid,
            "model_used": getattr(result, "_model", None) or "",
        }

    return generate


def _route_generate(state: RAGState) -> str:
    """Send the turn to the answer, refusal or error responder."""
    return {"answer": "respond_answer", "error": "respond_error"}.get(
        state["decision"], "not_in_document"
    )


def _turn(state: RAGState, reply: str) -> list[AnyMessage]:
    """The two messages (user question, assistant reply) appended to the conversation memory."""
    return [HumanMessage(state["question"]), AIMessage(reply)]


def _respond_answer(state: RAGState) -> dict:
    """Final state for an answer: rendered text with Sources and structured citations."""
    cits = citations.unique_citations(state["used"])
    return {
        "decision": "answer",
        "citations": [{"pages": list(c.pages), "section": c.section} for c in cits],
        "response": citations.render_answer(state["answer"], state["used"]),
        "messages": _turn(state, state["answer"]),
    }


def _not_in_document(state: RAGState) -> dict:
    """Final state for a refusal: 'not in the guide' plus the closest sections searched."""
    return {
        "decision": "not_in_document",
        "citations": [],
        "response": citations.render_refusal(NOT_IN_GUIDE, state.get("retrieved", [])),
        "messages": _turn(state, NOT_IN_GUIDE),
    }


ERROR_MESSAGES = {
    "rate_limit": RATE_LIMITED,
    "unavailable": SERVICE_UNAVAILABLE,
    "no_answer": ANSWER_FAILED,
}


def _respond_error(state: RAGState) -> dict:
    """Final state for a failure: a plain message chosen by error kind."""
    message = ERROR_MESSAGES.get(state.get("error_kind", ""), ANSWER_FAILED)
    return {
        "decision": "error",
        "response": citations.render_refusal(message, state.get("retrieved", [])),
        "messages": _turn(state, message),
    }


def _respond_empty(state: RAGState) -> dict:
    """Final state for a blank question: ask the user to type one (no API calls)."""
    return {"decision": "empty", "response": EMPTY_QUESTION}


# --------------------------------------------------------------------------- assembly


def build_graph(
    retriever: Retriever,
    answerer: Answerer,
    condenser: Condenser,
    *,
    top_k: int = 5,
    relevance_threshold: float = 0.0,
    checkpointer: Any = None,
):
    """Wire the nodes and edges into a compiled LangGraph with a checkpointer for memory."""
    g = StateGraph(RAGState)
    g.add_node("prepare", _prepare)
    g.add_node("condense", _make_condense(condenser))
    g.add_node("retrieve", _make_retrieve(retriever, top_k))
    g.add_node("generate", _make_generate(answerer))
    g.add_node("respond_answer", _respond_answer)
    g.add_node("not_in_document", _not_in_document)
    g.add_node("respond_error", _respond_error)
    g.add_node("respond_empty", _respond_empty)
    g.add_edge(START, "prepare")
    g.add_conditional_edges("prepare", _route_prepare, ["respond_empty", "condense"])
    g.add_edge("condense", "retrieve")
    g.add_conditional_edges(
        "retrieve",
        _make_route_retrieve(relevance_threshold),
        ["not_in_document", "generate", "respond_error"],
    )
    g.add_conditional_edges(
        "generate", _route_generate, ["respond_answer", "not_in_document", "respond_error"]
    )
    for node in ("respond_answer", "not_in_document", "respond_error", "respond_empty"):
        g.add_edge(node, END)
    return g.compile(checkpointer=checkpointer if checkpointer is not None else InMemorySaver())


class QdrantRetriever:
    """Embed the query with the collection's model and return payloads + scores."""

    def __init__(self, embedder: OpenRouterEmbeddings, client, collection: str) -> None:
        """Keep the embedder, the Qdrant client and the collection to search."""
        self.embedder, self.client, self.collection = embedder, client, collection

    def __call__(self, query: str, k: int) -> list[dict]:
        """Embed the query with the collection's model and return payloads plus scores."""
        hits = vectorstore.search(self.client, self.collection, self.embedder.embed_query(query), k)
        return [dict(h.payload or {}) | {"score": h.score, "chunk_id": str(h.id)} for h in hits]


def collection_for(settings: Settings) -> str:
    """The collection to query: QDRANT_COLLECTION if set, else derived from model + chunk config."""
    return settings.qdrant_collection or vectorstore.collection_name(
        settings.embedding_model, settings.chunk_config_hash
    )


def _query_cache(settings: Settings) -> EmbeddingCache | None:
    """An embedding cache for repeated questions, or None if the filesystem is read-only."""
    try:
        return EmbeddingCache(Path(settings.cache_dir) / "embeddings.sqlite")
    except Exception:  # read-only filesystem in a container: run without a cache
        return None


def build_rag(settings: Settings, checkpointer: Any = None):
    """Wire the real components (OpenRouter + Qdrant Cloud) into the graph."""
    embedder = OpenRouterEmbeddings(
        get_spec(settings.embedding_model),
        settings.openrouter_api_key.get_secret_value(),
        cache=_query_cache(settings),
    )
    client = vectorstore.make_client(
        settings.qdrant_url, settings.qdrant_api_key.get_secret_value()
    )
    chat = make_chat_model(settings)
    return build_graph(
        QdrantRetriever(embedder, client, collection_for(settings)),
        LLMAnswerer(chat),
        LLMCondenser(chat),
        top_k=settings.top_k,
        relevance_threshold=settings.relevance_threshold,
        checkpointer=checkpointer,
    )
