"""Terminal chat with the guide.

Usage:
  uv run python -m app.cli                      # interactive, one session (memory) until you quit
  uv run python -m app.cli -q "How do I close an app?" -q "And how do I reopen it?"
  uv run python -m app.cli --debug              # also print the rewrite, decision and pages

Every reply prints its Sources (page + section). Each question costs one query
embedding and one chat request (plus one to rewrite follow-ups) against the
free-model daily quota.
"""

from __future__ import annotations

import argparse
import sys
import uuid

import truststore

truststore.inject_into_ssl()

from app.config import ConfigError, get_settings  # noqa: E402
from app.graph import build_rag  # noqa: E402


def ask(graph, thread_id: str, question: str, debug: bool) -> dict:
    out = graph.invoke({"question": question}, {"configurable": {"thread_id": thread_id}})
    print(out["response"])
    if debug:
        pages = [h.get("page") for h in out.get("retrieved", [])]
        scores = [round(h.get("score", 0), 3) for h in out.get("retrieved", [])]
        print(
            f"  [debug] standalone={out.get('standalone_question')!r} decision={out['decision']} "
            f"retrieved_pages={pages} scores={scores} invalid_sources={out.get('invalid_sources')} "
            f"model={out.get('model_used') or '-'} error={out.get('error') or '-'}"
        )
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-q", "--question", action="append", help="ask non-interactively (repeatable)")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()
    try:
        settings = get_settings()
    except ConfigError as exc:
        print(exc)
        return 1
    graph = build_rag(settings)
    thread_id = str(uuid.uuid4())
    if args.question:
        for q in args.question:
            print(f"\n> {q}")
            ask(graph, thread_id, q, args.debug)
        return 0
    print("Ask about the iOS 7.1 iPhone User Guide (empty line or Ctrl+C to quit).")
    try:
        while question := input("\n> ").strip():
            ask(graph, thread_id, question, args.debug)
    except EOFError, KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
