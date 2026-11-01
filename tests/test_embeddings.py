"""OpenRouterEmbeddings against a mocked HTTP transport (no network, no quota used)."""

import json

import httpx
import pytest

from app.config import Settings
from app.embeddings import (
    REGISTRY,
    EmbeddingCache,
    EmbeddingError,
    EmbeddingSpec,
    OpenRouterEmbeddings,
    QuotaExhausted,
    get_spec,
)

SPEC = EmbeddingSpec(
    slug="test/model:free", dim=3, query_prefix="query: ", doc_prefix="passage: ", max_tokens=512
)


class FakeAPI:
    """Records requests; answers with deterministic 3-d vectors or scripted errors."""

    def __init__(self, script=None, max_batch=None):
        self.calls: list[list[str]] = []
        self.script = list(script or [])
        self.max_batch = max_batch

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.calls.append(body["input"])
        if self.script:
            status, payload, headers = self.script.pop(0)
            return httpx.Response(status, json=payload, headers=headers or {})
        if self.max_batch and len(body["input"]) > self.max_batch:
            return httpx.Response(400, json={"error": {"message": "too many inputs in batch"}})
        data = [
            {"index": i, "embedding": [float(len(t)), 1.0, 0.0]}
            for i, t in enumerate(body["input"])
        ]
        return httpx.Response(200, json={"data": data, "usage": {"prompt_tokens": 7}})


def make(api, **kw):
    return OpenRouterEmbeddings(
        SPEC, "sk-test", transport=httpx.MockTransport(api), sleep=lambda s: None, **kw
    )


def test_prefixes_are_applied_per_side():
    api = FakeAPI()
    emb = make(api)
    emb.embed_documents(["chunk"])
    emb.embed_query("question")
    assert api.calls == [["passage: chunk"], ["query: question"]]


def test_vectors_are_l2_normalised_and_usage_counted():
    emb = make(FakeAPI())
    (v,) = emb.embed_documents(["abc"])
    assert abs(sum(x * x for x in v) - 1.0) < 1e-9
    assert emb.prompt_tokens == 7 and emb.requests_made == 1


def test_batching_and_order():
    api = FakeAPI()
    emb = make(api, batch_size=2)
    vectors = emb.embed_documents(["a", "bb", "ccc"])
    assert [len(c) for c in api.calls] == [2, 1]
    assert [round(v[0] / v[1], 6) for v in vectors] == [
        len("passage: a"),
        len("passage: bb"),
        len("passage: ccc"),
    ]


def test_batch_size_adapts_when_the_api_rejects_large_batches():
    api = FakeAPI(max_batch=2)
    emb = make(api, batch_size=8)
    assert len(emb.embed_documents(list("abcde"))) == 5
    assert emb.batch_size == 2


def test_rate_limit_is_retried():
    api = FakeAPI(script=[(429, {"error": {"message": "Rate limit exceeded: per-minute"}}, None)])
    assert len(make(api).embed_documents(["a"])) == 1
    assert len(api.calls) == 2


def test_daily_quota_stops_immediately():
    msg = "Rate limit exceeded: free-models-per-day. Add 10 credits to unlock 1000 requests"
    api = FakeAPI(script=[(429, {"error": {"message": msg}}, None)])
    with pytest.raises(QuotaExhausted):
        make(api).embed_documents(["a"])
    assert len(api.calls) == 1


def test_error_inside_a_200_response_is_not_treated_as_data():
    api = FakeAPI(script=[(200, {"error": {"message": "provider unavailable"}}, None)] * 6)
    with pytest.raises(EmbeddingError):
        make(api).embed_documents(["a"])


def test_wrong_dimension_is_rejected():
    api = FakeAPI(script=[(200, {"data": [{"index": 0, "embedding": [1.0, 2.0]}]}, None)])
    with pytest.raises(EmbeddingError, match="expected dim 3"):
        make(api).embed_documents(["a"])


def test_cache_prevents_repeat_requests(tmp_path):
    cache = EmbeddingCache(tmp_path / "e.sqlite")
    api = FakeAPI()
    first = make(api, cache=cache).embed_documents(["a", "b"])
    second = make(api, cache=cache).embed_documents(["a", "b"])
    assert len(api.calls) == 1
    assert [[round(x, 5) for x in v] for v in first] == [[round(x, 5) for x in v] for v in second]
    cache.close()


def test_registry_specs_are_consistent():
    assert REGISTRY, "registry must not be empty"
    for slug, spec in REGISTRY.items():
        assert spec.slug == slug and slug.endswith(":free")
        assert spec.dim > 0 and spec.max_tokens >= 512
    with pytest.raises(ValueError, match="Unknown EMBEDDING_MODEL"):
        get_spec("nope/nope")


def test_default_embedding_model_is_registered_and_free():
    default = Settings.model_fields["embedding_model"].default
    assert default in REGISTRY and default.endswith(":free")
    assert Settings.model_fields["chat_model"].default.endswith(":free")
