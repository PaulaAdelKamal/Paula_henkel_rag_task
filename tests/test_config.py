import pytest
from pydantic import ValidationError

from app.config import Settings, describe_validation_error

REQUIRED = {
    "OPENROUTER_API_KEY": "sk-or-v1-test-secret-value",  # pragma: allowlist secret
    "QDRANT_URL": "https://example.cloud.qdrant.io:6333",
    "QDRANT_API_KEY": "qdrant-test-secret-value",  # pragma: allowlist secret
}


@pytest.fixture
def clean_env(monkeypatch):
    for var in [*REQUIRED, "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "TOP_K", "VISION_MODEL"]:
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def make_settings() -> Settings:
    return Settings(_env_file=None)  # never read the developer's real .env in tests


def test_defaults_only_need_secrets(clean_env):
    for k, v in REQUIRED.items():
        clean_env.setenv(k, v)
    s = make_settings()
    assert s.app_port == 8000
    assert s.top_k == 5
    assert s.langfuse_enabled is False


def test_missing_variable_is_named_without_values(clean_env):
    clean_env.setenv("QDRANT_URL", REQUIRED["QDRANT_URL"])
    clean_env.setenv("QDRANT_API_KEY", REQUIRED["QDRANT_API_KEY"])
    with pytest.raises(ValidationError) as exc_info:
        make_settings()
    message = describe_validation_error(exc_info.value)
    assert "OPENROUTER_API_KEY is missing" in message
    assert REQUIRED["QDRANT_API_KEY"] not in message


def test_invalid_value_message_does_not_echo_input(clean_env):
    for k, v in REQUIRED.items():
        clean_env.setenv(k, v)
    clean_env.setenv("TOP_K", "not-a-number")
    with pytest.raises(ValidationError) as exc_info:
        make_settings()
    message = describe_validation_error(exc_info.value)
    assert "TOP_K is invalid" in message
    assert "not-a-number" not in message


def test_empty_values_fall_back_to_defaults(clean_env):
    for k, v in REQUIRED.items():
        clean_env.setenv(k, v)
    clean_env.setenv("TOP_K", "")
    clean_env.setenv("VISION_MODEL", "")
    s = make_settings()
    assert s.top_k == 5
    assert s.vision_model is None


def test_secrets_are_masked_in_repr(clean_env):
    for k, v in REQUIRED.items():
        clean_env.setenv(k, v)
    text = repr(make_settings())
    assert REQUIRED["OPENROUTER_API_KEY"] not in text
    assert REQUIRED["QDRANT_API_KEY"] not in text


def test_chat_fallbacks_keep_only_free_models(clean_env):
    for k, v in REQUIRED.items():
        clean_env.setenv(k, v)
    clean_env.setenv("CHAT_FALLBACK_MODELS", "a/free-one:free, openai/gpt-5.6-terra ,b/two:free")
    s = make_settings()
    assert s.chat_models == [s.chat_model, "a/free-one:free", "b/two:free"]
