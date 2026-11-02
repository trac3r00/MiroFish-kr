"""LocalZepClient: the subset of the zep_cloud ``Zep`` client MiroFish calls.

Return objects are SimpleNamespaces carrying the zep_cloud field names
(``uuid_``, ``labels``, ``fact``, ...); missing resources raise
``zep_cloud.NotFoundError`` so service code handles both backends alike.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from zep_cloud import BadRequestError, NotFoundError
from zep_cloud.types.api_error import ApiError

from ..utils.locale import get_locale
from . import embed
from .store import GraphStore, utc_now
from .worker import GraphWorker

NEXT_CURSOR_HEADER = "zep-next-cursor"
MAX_SEARCH_LIMIT = 50
RRF_K = 60


def _not_found(message: str) -> NotFoundError:
    return NotFoundError(body=ApiError(message=message))


def _bad_request(message: str) -> BadRequestError:
    return BadRequestError(body=ApiError(message=message))


def _node(row: Dict[str, Any], **extra: Any) -> SimpleNamespace:
    return SimpleNamespace(
        uuid_=row["uuid"], graph_id=row["graph_id"], name=row["name"], labels=list(row["labels"]),
        summary=row["summary"], attributes=dict(row["attributes"]), created_at=row["created_at"], **extra,
    )


def _edge(row: Dict[str, Any], **extra: Any) -> SimpleNamespace:
    return SimpleNamespace(
        uuid_=row["uuid"], graph_id=row["graph_id"], name=row["name"], fact=row["fact"],
        source_node_uuid=row["source_node_uuid"], target_node_uuid=row["target_node_uuid"],
        attributes=dict(row["attributes"]), created_at=row["created_at"], valid_at=row["valid_at"],
        invalid_at=row["invalid_at"], expired_at=row["expired_at"], episodes=list(row["episodes"]), **extra,
    )


def _episode(row: Dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(
        uuid_=row["uuid"], graph_id=row["graph_id"], content=row["data"],
        source_description=row["source_description"], metadata=row["metadata"],
        created_at=row["created_at"], processed=bool(row["processed"]),
    )


def _timestamp(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _fields(model: Any) -> List[Dict[str, str]]:
    fields = getattr(model, "model_fields", {}) or {}
    return [
        {"name": name, "description": (info.description or "").strip()}
        for name, info in fields.items()
    ]


def _description(model: Any) -> str:
    return " ".join((getattr(model, "__doc__", None) or "").split())


class _Batch:
    def __init__(self, client: "LocalZepClient"):
        self._client = client
        self._store = client.store

    def _require(self, batch_id: str) -> Dict[str, Any]:
        batch = self._store.get_batch(batch_id)
        if batch is None:
            raise _not_found(f"batch {batch_id} not found")
        return batch

    def _summary(self, batch: Dict[str, Any]) -> SimpleNamespace:
        counts = self._store.batch_counts(batch["batch_id"])
        done = counts["succeeded"] + counts["failed"]
        percent = 100.0 if counts["total"] == 0 and batch["status"] != "draft" else (
            100.0 * done / counts["total"] if counts["total"] else 0.0
        )
        return SimpleNamespace(
            batch_id=batch["batch_id"], status=batch["status"], metadata=batch["metadata"],
            created_at=batch["created_at"],
            progress=SimpleNamespace(
                total_items=counts["total"], succeeded_items=counts["succeeded"],
                failed_items=counts["failed"], pending_items=counts["pending"],
                percent_complete=percent,
            ),
        )

    def create(self, *, metadata: Optional[Dict[str, Any]] = None, **_: Any) -> SimpleNamespace:
        return self._summary(self._store.create_batch(metadata))

    def add(self, batch_id: str, *, items: Sequence[Any], **_: Any) -> List[SimpleNamespace]:
        batch = self._require(batch_id)
        if batch["status"] != "draft":
            raise _bad_request(f"batch {batch_id} is {batch['status']}; items can only be added to a draft")
        locale = get_locale()
        prepared = []
        for item in items:
            if getattr(item, "type", None) != "graph_episode":
                raise _bad_request("only graph_episode batch items are supported")
            graph_id = getattr(item, "graph_id", None)
            if not graph_id or self._store.get_graph(graph_id) is None:
                raise _not_found(f"graph {graph_id} not found")
            data = getattr(item, "data", None)
            if not isinstance(data, str) or not data.strip():
                raise _bad_request("batch item data must be non-empty text")
            prepared.append({
                "graph_id": graph_id, "data": data,
                "source_description": getattr(item, "source_description", None),
                "metadata": getattr(item, "metadata", None) or {},
                "created_at": _timestamp(getattr(item, "created_at", None)),
                "locale": locale,
            })
        return [self._item(row) for row in self._store.add_batch_items(batch_id, prepared)]

    @staticmethod
    def _item(row: Dict[str, Any]) -> SimpleNamespace:
        return SimpleNamespace(
            batch_id=row["batch_id"], sequence_index=row["sequence_index"],
            episode_uuid=row["episode_uuid"], source_uuid=row["episode_uuid"],
            graph_id=row["graph_id"], status=row["status"], error=row["error"],
        )

    def process(self, batch_id: str, **_: Any) -> SimpleNamespace:
        batch = self._require(batch_id)
        if batch["status"] != "draft":
            raise _bad_request(f"batch {batch_id} is already {batch['status']}")
        rows, _cursor = self._store.batch_items(batch_id)
        self._store.set_batch_status(batch_id, "processing" if rows else "succeeded")
        self._client.worker.enqueue(row["episode_uuid"] for row in rows)
        return self._summary(self._require(batch_id))

    def get(self, batch_id: str, **_: Any) -> SimpleNamespace:
        return self._summary(self._require(batch_id))

    def list_items(self, batch_id: str, *, limit: Optional[int] = None, cursor: Optional[int] = None, **_: Any) -> SimpleNamespace:
        self._require(batch_id)
        rows, next_cursor = self._store.batch_items(batch_id, limit or 100, cursor)
        return SimpleNamespace(items=[self._item(row) for row in rows], next_cursor=next_cursor)

    def list(self, *, limit: Optional[int] = None, cursor: Optional[int] = None, **_: Any) -> SimpleNamespace:
        rows, next_cursor = self._store.list_batches(limit or 100, cursor)
        return SimpleNamespace(batches=[self._summary(row) for row in rows], next_cursor=next_cursor)


class _RawPager:
    def __init__(self, owner: "_Pager"):
        self._owner = owner

    def get_by_graph_id(self, graph_id: str, *, limit: Optional[int] = None, cursor: Optional[str] = None, **_: Any) -> SimpleNamespace:
        data, next_cursor = self._owner._page(graph_id, limit, cursor)
        headers = {NEXT_CURSOR_HEADER: next_cursor} if next_cursor is not None else {}
        return SimpleNamespace(data=data, headers=headers)


class _Pager:
    kind = ""

    def __init__(self, client: "LocalZepClient"):
        self._client = client
        self._store = client.store
        self.with_raw_response = _RawPager(self)

    def _page(self, graph_id: str, limit: Optional[int], cursor: Optional[str]):
        self._client.graph._require(graph_id)
        try:
            rows, next_cursor = self._store.page(self.kind, graph_id, limit, cursor)
        except ValueError as error:
            raise _bad_request(str(error)) from error
        wrap = _node if self.kind == "nodes" else _edge
        return [wrap(row) for row in rows], next_cursor

    def get_by_graph_id(self, graph_id: str, *, limit: Optional[int] = None, cursor: Optional[str] = None, **_: Any) -> List[SimpleNamespace]:
        return self._page(graph_id, limit, cursor)[0]


class _Node(_Pager):
    kind = "nodes"

    def get(self, uuid_: str, **_: Any) -> SimpleNamespace:
        row = self._store.get_node(uuid_)
        if row is None:
            raise _not_found(f"node {uuid_} not found")
        return _node(row)

    def get_edges(self, node_uuid: str, **_: Any) -> List[SimpleNamespace]:
        self.get(node_uuid)
        return [_edge(row) for row in self._store.edges_of_node(node_uuid)]


class _Edge(_Pager):
    kind = "edges"

    def get(self, uuid_: str, **_: Any) -> SimpleNamespace:
        row = self._store.get_edge(uuid_)
        if row is None:
            raise _not_found(f"edge {uuid_} not found")
        return _edge(row)


class _Episode:
    def __init__(self, client: "LocalZepClient"):
        self._store = client.store

    def get(self, uuid_: str, **_: Any) -> SimpleNamespace:
        row = self._store.get_episode(uuid_)
        if row is None:
            raise _not_found(f"episode {uuid_} not found")
        return _episode(row)


class _Graph:
    def __init__(self, client: "LocalZepClient"):
        self._client = client
        self._store = client.store
        self.node = _Node(client)
        self.edge = _Edge(client)
        self.episode = _Episode(client)

    def _require(self, graph_id: str) -> Dict[str, Any]:
        graph = self._store.get_graph(graph_id)
        if graph is None:
            raise _not_found(f"graph {graph_id} not found")
        return graph

    @staticmethod
    def _graph(row: Dict[str, Any]) -> SimpleNamespace:
        return SimpleNamespace(
            graph_id=row["graph_id"], name=row["name"], description=row["description"],
            created_at=row["created_at"],
        )

    def create(self, graph_id: str, *, name: Optional[str] = None, description: Optional[str] = None, **_: Any) -> SimpleNamespace:
        if not graph_id:
            raise _bad_request("graph_id is required")
        if self._store.get_graph(graph_id) is not None:
            raise _bad_request(f"graph {graph_id} already exists")
        return self._graph(self._store.create_graph(graph_id, name, description))

    def get(self, graph_id: str, **_: Any) -> SimpleNamespace:
        return self._graph(self._require(graph_id))

    def delete(self, graph_id: str, **_: Any) -> SimpleNamespace:
        self._require(graph_id)
        self._store.delete_graph(graph_id)
        return SimpleNamespace(message="deleted")

    def set_ontology(
        self,
        *,
        entities: Optional[Dict[str, Any]] = None,
        edges: Optional[Dict[str, Any]] = None,
        graph_ids: Optional[Iterable[str]] = None,
        **_: Any,
    ) -> SimpleNamespace:
        ids = list(graph_ids or [])
        if not ids:
            raise _bad_request("graph_ids is required for the local graph backend")
        for graph_id in ids:
            self._require(graph_id)
        ontology = {
            "entity_types": [
                {"name": name, "description": _description(model), "attributes": _fields(model)}
                for name, model in (entities or {}).items()
            ],
            "edge_types": [
                {
                    "name": name,
                    "description": _description(definition[0]),
                    "attributes": _fields(definition[0]),
                    "source_targets": [
                        {"source": getattr(st, "source", None) or "Entity",
                         "target": getattr(st, "target", None) or "Entity"}
                        for st in (definition[1] or [])
                    ],
                }
                for name, definition in (edges or {}).items()
            ],
        }
        for graph_id in ids:
            self._store.set_ontology(graph_id, ontology)
        return SimpleNamespace(message="ontology set")

    def add(
        self,
        graph_id: str,
        *,
        data: str,
        type: str = "text",
        created_at: Any = None,
        source_description: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **_: Any,
    ) -> SimpleNamespace:
        self._require(graph_id)
        if not isinstance(data, str) or not data.strip():
            raise _bad_request("episode data must be non-empty text")
        row = self._store.add_episode(
            graph_id, data, source_description=source_description, metadata=metadata,
            created_at=_timestamp(created_at) or utc_now(), locale=get_locale(),
        )
        self._client.worker.enqueue([row["uuid"]])
        return _episode(row)

    def search(
        self,
        *,
        query: str,
        graph_id: Optional[str] = None,
        limit: Optional[int] = None,
        scope: Optional[str] = None,
        reranker: Optional[str] = None,
        **_: Any,
    ) -> SimpleNamespace:
        if not graph_id:
            raise _bad_request("graph_id is required for the local graph backend")
        self._require(graph_id)
        if not isinstance(query, str) or not query.strip():
            raise _bad_request("query must not be empty")
        size = max(1, min(int(limit or 10), MAX_SEARCH_LIMIT))
        scope = scope or "edges"
        if scope == "episodes":
            return SimpleNamespace(edges=None, nodes=None, episodes=[])
        if scope not in ("edges", "nodes"):
            raise _bad_request(f"unsupported search scope {scope!r}")
        ranked = self._rank(scope, graph_id, query, size)
        rows = self._store.rows_by_uuid(scope, [u for u, _ in ranked])
        scores = dict(ranked)
        wrap = _node if scope == "nodes" else _edge
        results = [wrap(row, score=scores[row["uuid"]]) for row in rows]
        if scope == "nodes":
            return SimpleNamespace(nodes=results, edges=None, episodes=None)
        return SimpleNamespace(edges=results, nodes=None, episodes=None)

    def _rank(self, kind: str, graph_id: str, query: str, size: int) -> List[Tuple[str, float]]:
        pool = size * 3
        rankings = [self._store.lexical_search(kind, graph_id, query, pool)]
        stored = self._store.embeddings(kind, graph_id) if embed.status()["enabled"] else []
        if stored:
            vector = embed.embed_query(query)
            if vector is not None:
                rankings.append([u for u, _ in embed.rank_by_cosine(vector, stored, pool)])
        fused: Dict[str, float] = {}
        for ranking in rankings:
            for position, row_uuid in enumerate(ranking):
                fused[row_uuid] = fused.get(row_uuid, 0.0) + 1.0 / (RRF_K + position + 1)
        return sorted(fused.items(), key=lambda kv: (-kv[1], kv[0]))[:size]


class LocalZepClient:
    """Drop-in for ``zep_cloud.client.Zep`` backed by SQLite (see package docs)."""

    def __init__(self, store: Optional[GraphStore] = None, *, db_path: Optional[str] = None, worker: Optional[GraphWorker] = None):
        self.store = store or GraphStore(db_path)
        self.worker = worker or GraphWorker(self.store)
        self.graph = _Graph(self)
        self.batch = _Batch(self)
        self.worker.resume_pending()
