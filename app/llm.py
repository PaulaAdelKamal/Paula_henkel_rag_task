"""Chat model access (OpenRouter via LangChain) and the two LLM-backed steps.

The free chat model (google/gemma-4-31b-it:free) supports tool calling but not
strict JSON-schema output on OpenRouter, so the grounded answer is requested
as a function call (`with_structured_output(method="function_calling")`). If
the model answers in plain text instead, a JSON object in the text is accepted;
otherwise the request is retried once, and a second miss is reported as a
failure rather than guessed at.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence

from langchain_core.messages import AIMessage, BaseMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field, PrivateAttr, ValidationError

from app.config import OPENROUTER_BASE_URL, Settings
from app.prompts import ANSWER_PROMPT, CONDENSE_PROMPT, format_sources


class GroundedAnswer(BaseModel):
    """The model's answer, restricted to the numbered sources it was given."""

    answerable: bool = Field(description="True only if the sources contain the answer.")
    answer: str = Field(default="", description="The answer, using only the sources.")
    used_sources: list[int] = Field(
        default_factory=list, description="Numbers of the sources the answer uses, e.g. [1, 3]."
    )
    _model: str | None = PrivateAttr(default=None)  # model that actually answered (not in schema)


class AnswerUnavailable(RuntimeError):
    """The model did not return a usable structured answer."""


def make_chat_model(settings: Settings, **overrides) -> ChatOpenAI:
    """ChatOpenAI pointed at OpenRouter, temperature 0, with the free fallback chain."""
    params = {
        "model": settings.chat_model,
        "api_key": settings.openrouter_api_key.get_secret_value(),
        "base_url": OPENROUTER_BASE_URL,
        "temperature": 0,
        "max_retries": 2,
        "timeout": 60,
        "default_headers": {"X-OpenRouter-Title": "iphone-guide-rag"},
    }
    if len(settings.chat_models) > 1:
        # OpenRouter tries these in order when the previous one errors (incl. rate limits).
        params["extra_body"] = {"models": settings.chat_models}
    return ChatOpenAI(**(params | overrides))


def parse_answer(result: dict) -> GroundedAnswer:
    """Accept the tool-call result, else a JSON object in the raw text, else fail."""
    raw = result.get("raw")
    model = raw.response_metadata.get("model_name") if isinstance(raw, AIMessage) else None
    if isinstance(result.get("parsed"), GroundedAnswer):
        result["parsed"]._model = model
        return result["parsed"]
    text = raw.content if isinstance(raw, AIMessage) and isinstance(raw.content, str) else ""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            parsed = GroundedAnswer.model_validate(json.loads(match.group(0)))
            parsed._model = model
            return parsed
        except ValueError, ValidationError:
            pass
    raise AnswerUnavailable("no structured answer in the model output")


class LLMAnswerer:
    """question + retrieved chunks -> GroundedAnswer (one chat request, two at most)."""

    # Free models occasionally ignore the forced function call and reply in prose (seen
    # live in the red-team run). One retry recovers that; a second miss is reported.
    ATTEMPTS = 2

    def __init__(self, chat_model: ChatOpenAI) -> None:
        """Compose prompt -> chat model with GroundedAnswer as a function-call schema."""
        structured = chat_model.with_structured_output(
            GroundedAnswer, method="function_calling", include_raw=True
        )
        self._chain = ANSWER_PROMPT | structured

    def __call__(self, question: str, chunks: list[dict], config=None) -> GroundedAnswer:
        """Answer one question from numbered sources; retry once if no structure comes back."""
        inputs = {"question": question, "sources": format_sources(chunks)}
        for attempt in range(1, self.ATTEMPTS + 1):
            try:
                return parse_answer(self._chain.invoke(inputs, config=config))
            except AnswerUnavailable:
                if attempt == self.ATTEMPTS:
                    raise
        raise AssertionError("unreachable")  # the loop always returns or raises


class LLMCondenser:
    """Rewrite a follow-up into a standalone question (one chat request, only with history)."""

    def __init__(self, chat_model: ChatOpenAI) -> None:
        """Compose the condense prompt with the chat model."""
        self._chain = CONDENSE_PROMPT | chat_model

    def __call__(self, history: Sequence[BaseMessage], question: str, config=None) -> str:
        """Rewrite the latest message into a standalone question from the transcript."""
        transcript = "\n".join(
            f"{'User' if m.type == 'human' else 'Assistant'}: {m.content}" for m in history
        )
        out = self._chain.invoke({"history": transcript, "question": question}, config=config)
        text = out.content if isinstance(out.content, str) else question
        return text.strip().strip('"') or question
