"""app/vectorstore.py against Qdrant's in-memory mode (no cloud cluster needed)."""

import uuid

import pytest
from qdrant_client import QdrantClient

from app import vectorstore


def chunk(i: int, page: int = 22) -> dict:
    return {
        "chunk_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"chunk-{i}")),
        "page": page,
        "pages": [page],
        "chapter_number": "3",
        "content_type": "text",
        "has_figure": False,
        "text": f"text {i}",
        "embed_text": f"header\ntext {i}",
    }


@pytest.fixture
def client():
    return QdrantClient(":memory:")


def test_collection_name_is_sanitised():
    name = vectorstore.collection_name("nvidia/nemotron-3.5-embed-1b:free", "edb94b731b")
    assert name == "iphone_guide__nvidia_nemotron_3_5_embed_1b_free__edb94b731b"


def test_sync_is_idempotent_and_removes_stale_points(client):
    name = "c"
    assert vectorstore.ensure_collection(client, name, dim=2) is True
    assert vectorstore.ensure_collection(client, name, dim=2) is False
    first = [chunk(1), chunk(2), chunk(3)]
    vectorstore.upsert(client, name, first, [[1.0, 0.0]] * 3, "m")
    present = vectorstore.existing_ids(client, name)
    assert vectorstore.plan_sync([c["chunk_id"] for c in first], present) == ([], [])

    second = [chunk(2), chunk(3), chunk(4)]  # chunk 1 changed into chunk 4
    to_add, to_delete = vectorstore.plan_sync([c["chunk_id"] for c in second], present)
    assert to_add == [chunk(4)["chunk_id"]] and to_delete == [chunk(1)["chunk_id"]]
    vectorstore.upsert(client, name, [chunk(4)], [[0.0, 1.0]], "m")
    vectorstore.delete(client, name, to_delete)
    assert vectorstore.existing_ids(client, name) == {c["chunk_id"] for c in second}


def test_payload_drops_embed_text_and_records_model(client):
    vectorstore.ensure_collection(client, "c", dim=2)
    vectorstore.upsert(client, "c", [chunk(1)], [[1.0, 0.0]], "test/model")
    (hit,) = vectorstore.search(client, "c", [1.0, 0.0], k=1)
    assert "embed_text" not in hit.payload
    assert hit.payload["embedding_model"] == "test/model" and hit.payload["page"] == 22


def test_dimension_mismatch_is_refused(client):
    vectorstore.ensure_collection(client, "c", dim=2)
    with pytest.raises(ValueError, match="dim 2"):
        vectorstore.ensure_collection(client, "c", dim=3)


def test_status_reports_problems(client):
    missing = vectorstore.status(client, "absent")
    assert missing.problems() == ["collection absent does not exist"]
    vectorstore.ensure_collection(client, "c", dim=2)
    empty = vectorstore.status(client, "c")
    assert "is empty" in empty.problems()[0]
    vectorstore.upsert(client, "c", [chunk(1)], [[1.0, 0.0]], "m")
    st = vectorstore.status(client, "c")
    assert st.problems(expected_dim=2, expected_points=1) == []
    assert st.problems(expected_dim=4, expected_points=2) == [
        "c has 1 points, expected 2",
        "c has dim 2, embedding model gives 4",
    ]
