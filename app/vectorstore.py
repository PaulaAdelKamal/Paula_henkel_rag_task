"""Qdrant access: collection naming, idempotent sync, search, health checks.

One collection per (embedding model, chunk config), because dimensions differ
per model and the benchmark compares models on identical chunks:
    iphone_guide__{model_slug_sanitized}__{chunk_config_hash}
Point ids are the chunks' deterministic UUIDv5s, so syncing is idempotent:
missing points are added, stale ones deleted, unchanged ones untouched.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from qdrant_client import QdrantClient, models

COLLECTION_PREFIX = "iphone_guide"
# Payload fields filtered on at query time (and in evaluation slices).
PAYLOAD_INDEXES = {
    "page": models.PayloadSchemaType.INTEGER,
    "pages": models.PayloadSchemaType.INTEGER,
    "chapter_number": models.PayloadSchemaType.KEYWORD,
    "content_type": models.PayloadSchemaType.KEYWORD,
    "has_figure": models.PayloadSchemaType.BOOL,
}
# Chunk fields not stored in the payload (embed_text is reproducible from header + text).
PAYLOAD_EXCLUDE = {"embed_text"}


def collection_name(embedding_slug: str, chunk_config_hash: str) -> str:
    """Collection name for a model + chunk config, e.g. iphone_guide__nvidia_…__edb94b731b."""
    slug = re.sub(r"[^a-z0-9]+", "_", embedding_slug.lower()).strip("_")
    return f"{COLLECTION_PREFIX}__{slug}__{chunk_config_hash}"


def make_client(url: str, api_key: str | None, timeout: int = 30) -> QdrantClient:
    """Qdrant client for the cloud cluster."""
    return QdrantClient(url=url, api_key=api_key, timeout=timeout)


def ensure_collection(client: QdrantClient, name: str, dim: int) -> bool:
    """Create the collection (cosine) and its payload indexes. Returns True if created."""
    if client.collection_exists(name):
        existing = client.get_collection(name).config.params.vectors
        if existing.size != dim:
            raise ValueError(f"collection {name} has dim {existing.size}, model gives {dim}")
        return False
    client.create_collection(
        name, vectors_config=models.VectorParams(size=dim, distance=models.Distance.COSINE)
    )
    for field, schema in PAYLOAD_INDEXES.items():
        client.create_payload_index(name, field_name=field, field_schema=schema)
    return True


def existing_ids(client: QdrantClient, name: str) -> set[str]:
    """All point ids currently in a collection (scrolled in pages of 1,000)."""
    ids: set[str] = set()
    offset = None
    while True:
        points, offset = client.scroll(
            name, limit=1000, offset=offset, with_payload=False, with_vectors=False
        )
        ids.update(str(p.id) for p in points)
        if offset is None:
            return ids


def plan_sync(chunk_ids: Sequence[str], present: set[str]) -> tuple[list[str], list[str]]:
    """(ids to add, ids to delete) to make the collection match the chunks exactly."""
    wanted = set(chunk_ids)
    return [i for i in chunk_ids if i not in present], sorted(present - wanted)


def payload(chunk: dict, embedding_model: str, ingested_at: str | None = None) -> dict:
    """The stored payload of a chunk: its metadata minus embed_text, plus model and timestamp."""
    out = {k: v for k, v in chunk.items() if k not in PAYLOAD_EXCLUDE}
    out["embedding_model"] = embedding_model
    out["ingested_at"] = ingested_at or datetime.now(UTC).isoformat(timespec="seconds")
    return out


def upsert(
    client: QdrantClient,
    name: str,
    chunks: Sequence[dict],
    vectors: Sequence[list[float]],
    embedding_model: str,
    batch: int = 64,
) -> None:
    """Write chunks and their vectors as points (id = chunk_id), in batches."""
    now = datetime.now(UTC).isoformat(timespec="seconds")
    points = [
        models.PointStruct(id=c["chunk_id"], vector=v, payload=payload(c, embedding_model, now))
        for c, v in zip(chunks, vectors, strict=True)
    ]
    for i in range(0, len(points), batch):
        client.upsert(name, points=points[i : i + batch], wait=True)


def delete(client: QdrantClient, name: str, ids: Sequence[str]) -> None:
    """Remove points by id (stale chunks after a re-chunk)."""
    if ids:
        client.delete(name, points_selector=models.PointIdsList(points=list(ids)), wait=True)


def search(
    client: QdrantClient, name: str, vector: list[float], k: int, query_filter=None
) -> list[models.ScoredPoint]:
    """Nearest chunks to a query vector, with payloads and cosine scores."""
    return client.query_points(
        name, query=vector, limit=k, with_payload=True, query_filter=query_filter
    ).points


@dataclass
class CollectionStatus:
    """What a health check needs to know about one collection."""

    name: str
    exists: bool
    points: int = 0
    dim: int | None = None

    def problems(self, expected_dim: int | None = None, expected_points: int | None = None):
        """Human-readable problems (missing, empty, wrong size or dimension); [] if healthy."""
        if not self.exists:
            return [f"collection {self.name} does not exist"]
        out = []
        if self.points == 0:
            out.append(f"collection {self.name} is empty")
        if expected_points is not None and self.points != expected_points:
            out.append(f"{self.name} has {self.points} points, expected {expected_points}")
        if expected_dim is not None and self.dim != expected_dim:
            out.append(f"{self.name} has dim {self.dim}, embedding model gives {expected_dim}")
        return out


def status(client: QdrantClient, name: str) -> CollectionStatus:
    """Inspect a collection: existence, point count and vector dimension."""
    if not client.collection_exists(name):
        return CollectionStatus(name, exists=False)
    info = client.get_collection(name)
    return CollectionStatus(
        name,
        exists=True,
        points=client.count(name, exact=True).count,
        dim=info.config.params.vectors.size,
    )
