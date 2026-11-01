from app.config import Settings
from scripts.check_connections import check_langfuse, redact, run_check

SECRET = "sk-or-v1-very-secret"  # pragma: allowlist secret


def settings(**overrides) -> Settings:
    values = {
        "openrouter_api_key": SECRET,
        "qdrant_url": "https://example.cloud.qdrant.io:6333",
        "qdrant_api_key": "qdrant-secret",  # pragma: allowlist secret
        "langfuse_public_key": None,
        "langfuse_secret_key": None,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_redact_removes_every_secret():
    s = settings(langfuse_public_key="pk-lf-abc", langfuse_secret_key="sk-lf-xyz")
    msg = f"auth failed for {SECRET} / qdrant-secret / pk-lf-abc / sk-lf-xyz"
    assert redact(msg, s) == "auth failed for *** / *** / *** / ***"


def test_failure_line_never_contains_the_key():
    def boom(_s):
        raise RuntimeError(f"401 Unauthorized: Bearer {SECRET}")

    result = run_check("OpenRouter", boom, settings())
    assert result.status == "FAIL"
    assert SECRET not in result.detail
    assert "RuntimeError" in result.detail


def test_langfuse_is_skipped_without_keys():
    assert check_langfuse(settings()).status == "SKIP"
