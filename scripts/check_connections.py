"""Verify that OpenRouter, Qdrant Cloud and Langfuse are reachable with the keys in .env.

Usage (from the repo root):  uv run python -m scripts.check_connections

Runs the same checks as the UI's startup self-check (app/health.py): the
OpenRouter key and models, the Qdrant collection for EMBEDDING_MODEL (exists,
non-empty, right dimension) and, optionally, Langfuse. Prints one line per
service, never a key value. Exit code 0 unless a required check fails.
"""

from __future__ import annotations

import sys

import truststore

truststore.inject_into_ssl()  # use the OS trust store (works behind TLS-inspecting proxies)

from app.config import ConfigError, get_settings  # noqa: E402
from app.health import startup_checks  # noqa: E402


def main() -> int:
    try:
        settings = get_settings()
    except ConfigError as exc:
        print(exc)
        return 1
    checks = startup_checks(settings)
    for c in checks:
        print(f"[{c.status:<4}] {c.service:<10} {c.detail}")
    return 1 if any(c.blocking for c in checks) else 0


if __name__ == "__main__":
    sys.exit(main())
