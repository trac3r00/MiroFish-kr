"""SQLite persistence for the local Zep-compatible graph backend.

One database file (WAL mode) holds every graph. A single shared connection is
guarded by an RLock, so the API threads and the extraction worker can share
one store safely. FTS5 ``trigram`` tables index node name+summary and edge
fact text; trigram works for Korean/Chinese where word tokenizers fail.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import unicodedata
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

DEFAULT_PAGE_LIMIT = 100

_SCHEMA = """
CREATE TABLE IF NOT EXISTS graphs (
    graph_id TEXT PRIMARY KEY,
    name TEXT,
    description TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ontologies (
    graph_id TEXT PRIMARY KEY,
    ontology_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid TEXT NOT NULL UNIQUE,
    graph_id TEXT NOT NULL,
    data TEXT NOT NULL,
    source_description TEXT,
    metadata TEXT NOT NULL DEFAULT '{}',
    locale TEXT,
    created_at TEXT NOT NULL,
    processed INTEGER NOT NULL DEFAULT 0,
    queued INTEGER NOT NULL DEFAULT 0,
    error TEXT
);
CREATE INDEX IF NOT EXISTS episodes_graph ON episodes(graph_id);
CREATE TABLE IF NOT EXISTS nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid TEXT NOT NULL UNIQUE,
    graph_id TEXT NOT NULL,
    name TEXT NOT NULL,
    norm_name TEXT NOT NULL,
    labels TEXT NOT NULL DEFAULT '["Entity"]',
    summary TEXT NOT NULL DEFAULT '',
    attributes TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    embedding BLOB,
    name_embedding BLOB
);
CREATE INDEX IF NOT EXISTS nodes_graph ON nodes(graph_id, id);
CREATE INDEX IF NOT EXISTS nodes_norm ON nodes(graph_id, norm_name);
CREATE TABLE IF NOT EXISTS edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid TEXT NOT NULL UNIQUE,
    graph_id TEXT NOT NULL,
    name TEXT NOT NULL,
    fact TEXT NOT NULL,
    source_node_uuid TEXT NOT NULL,
    target_node_uuid TEXT NOT NULL,
    attributes TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    valid_at TEXT,
    invalid_at TEXT,
    expired_at TEXT,
    episodes TEXT NOT NULL DEFAULT '[]',
    embedding BLOB
);
CREATE INDEX IF NOT EXISTS edges_graph ON edges(graph_id, id);
CREATE INDEX IF NOT EXISTS edges_source ON edges(source_node_uuid);
CREATE INDEX IF NOT EXISTS edges_target ON edges(target_node_uuid);
CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL UNIQUE,
    metadata TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS batch_items (
    batch_id TEXT NOT NULL,
    sequence_index INTEGER NOT NULL,
    episode_uuid TEXT NOT NULL,
    graph_id TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    PRIMARY KEY (batch_id, sequence_index)
);
CREATE INDEX IF NOT EXISTS batch_items_episode ON batch_items(episode_uuid);
CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
    uuid UNINDEXED, graph_id UNINDEXED, text, tokenize='trigram'
);
CREATE VIRTUAL TABLE IF NOT EXISTS edges_fts USING fts5(
    uuid UNINDEXED, graph_id UNINDEXED, text, tokenize='trigram'
);
"""

_TERM_SPLIT = re.compile(r"[\s\.,;:!?\"'`()\[\]{}<>/\\|·…“”‘’「」『』、。，！？：；]+")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def new_uuid() -> str:
    return str(uuid.uuid4())


def normalize_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value or "").casefold().split())


def query_terms(query: str) -> List[str]:
    seen: List[str] = []
    for term in _TERM_SPLIT.split(normalize_text(query)):
        if term and term not in seen:
            seen.append(term)
    return seen


def _loads(value: Optional[str], default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def default_db_path() -> str:
    explicit = os.environ.get("GRAPH_LOCAL_DB")
    if explicit:
        return explicit
    from ..config import Config

    return os.path.join(Config.UPLOAD_FOLDER, "graph_local.db")


class GraphStore:
    def __init__(self, path: Optional[str] = None):
        self.path = path or default_db_path()
        if self.path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.path, check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=OFF")
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _query(self, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self._conn.execute(sql, params).fetchall()]

    def _one(self, sql: str, params: Sequence[Any] = ()) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def transaction(self):
        return _Transaction(self)

    def create_graph(self, graph_id: str, name: Optional[str], description: Optional[str]) -> Dict[str, Any]:
        created_at = utc_now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO graphs(graph_id, name, description, created_at) VALUES (?,?,?,?)",
                (graph_id, name, description, created_at),
            )
        return {"graph_id": graph_id, "name": name, "description": description, "created_at": created_at}

    def get_graph(self, graph_id: str) -> Optional[Dict[str, Any]]:
        return self._one("SELECT * FROM graphs WHERE graph_id=?", (graph_id,))

    def delete_graph(self, graph_id: str) -> bool:
        with self.transaction() as conn:
            deleted = conn.execute("DELETE FROM graphs WHERE graph_id=?", (graph_id,)).rowcount
            for table in ("ontologies", "episodes", "nodes", "edges", "nodes_fts", "edges_fts"):
                conn.execute(f"DELETE FROM {table} WHERE graph_id=?", (graph_id,))
        return bool(deleted)

    def set_ontology(self, graph_id: str, ontology: Dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO ontologies(graph_id, ontology_json) VALUES (?,?) "
                "ON CONFLICT(graph_id) DO UPDATE SET ontology_json=excluded.ontology_json",
                (graph_id, _dumps(ontology)),
            )

    def get_ontology(self, graph_id: str) -> Dict[str, Any]:
        row = self._one("SELECT ontology_json FROM ontologies WHERE graph_id=?", (graph_id,))
        return _loads(row["ontology_json"], {}) if row else {}

    def add_episode(
        self,
        graph_id: str,
        data: str,
        *,
        source_description: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        created_at: Optional[str] = None,
        locale: Optional[str] = None,
        conn: Optional[sqlite3.Connection] = None,
    ) -> Dict[str, Any]:
        row = {
            "uuid": new_uuid(),
            "graph_id": graph_id,
            "data": data,
            "source_description": source_description,
            "metadata": metadata or {},
            "locale": locale,
            "created_at": created_at or utc_now(),
            "processed": False,
        }
        sql = (
            "INSERT INTO episodes(uuid, graph_id, data, source_description, metadata, locale, created_at) "
            "VALUES (?,?,?,?,?,?,?)"
        )
        params = (
            row["uuid"], graph_id, data, source_description,
            _dumps(row["metadata"]), locale, row["created_at"],
        )
        if conn is not None:
            conn.execute(sql, params)
        else:
            with self._lock:
                self._conn.execute(sql, params)
        return row

    def get_episode(self, episode_uuid: str) -> Optional[Dict[str, Any]]:
        row = self._one("SELECT * FROM episodes WHERE uuid=?", (episode_uuid,))
        if row:
            row["metadata"] = _loads(row["metadata"], {})
            row["processed"] = bool(row["processed"])
        return row

    def mark_episodes_queued(self, episode_uuids: Iterable[str]) -> None:
        with self.transaction() as conn:
            conn.executemany(
                "UPDATE episodes SET queued=1 WHERE uuid=?", [(u,) for u in episode_uuids]
            )

    def mark_episode_processed(self, episode_uuid: str, error: Optional[str]) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE episodes SET processed=1, error=? WHERE uuid=?", (error, episode_uuid)
            )

    def pending_queued_episodes(self) -> List[str]:
        rows = self._query("SELECT uuid FROM episodes WHERE queued=1 AND processed=0 ORDER BY id")
        return [row["uuid"] for row in rows]

    @staticmethod
    def _node_out(row: Dict[str, Any]) -> Dict[str, Any]:
        row["labels"] = _loads(row["labels"], ["Entity"])
        row["attributes"] = _loads(row["attributes"], {})
        return row

    @staticmethod
    def _edge_out(row: Dict[str, Any]) -> Dict[str, Any]:
        row["attributes"] = _loads(row["attributes"], {})
        row["episodes"] = _loads(row["episodes"], [])
        return row

    def find_node_by_norm(self, conn: sqlite3.Connection, graph_id: str, norm_name: str) -> Optional[Dict[str, Any]]:
        row = conn.execute(
            "SELECT * FROM nodes WHERE graph_id=? AND norm_name=? ORDER BY id LIMIT 1",
            (graph_id, norm_name),
        ).fetchone()
        return self._node_out(dict(row)) if row else None

    def node_name_embeddings(self, conn: sqlite3.Connection, graph_id: str) -> List[Tuple[str, bytes]]:
        rows = conn.execute(
            "SELECT uuid, name_embedding FROM nodes WHERE graph_id=? AND name_embedding IS NOT NULL",
            (graph_id,),
        ).fetchall()
        return [(row["uuid"], row["name_embedding"]) for row in rows]

    def insert_node(self, conn: sqlite3.Connection, node: Dict[str, Any]) -> None:
        cursor = conn.execute(
            "INSERT INTO nodes(uuid, graph_id, name, norm_name, labels, summary, attributes, created_at, "
            "embedding, name_embedding) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                node["uuid"], node["graph_id"], node["name"], normalize_text(node["name"]),
                _dumps(node["labels"]), node["summary"], _dumps(node["attributes"]),
                node["created_at"], node.get("embedding"), node.get("name_embedding"),
            ),
        )
        self._index(conn, "nodes_fts", cursor.lastrowid, node, f"{node['name']}\n{node['summary']}")

    def update_node(self, conn: sqlite3.Connection, node: Dict[str, Any]) -> None:
        conn.execute(
            "UPDATE nodes SET labels=?, summary=?, attributes=?, embedding=COALESCE(?, embedding) WHERE uuid=?",
            (
                _dumps(node["labels"]), node["summary"], _dumps(node["attributes"]),
                node.get("embedding"), node["uuid"],
            ),
        )
        self._index(conn, "nodes_fts", node["id"], node, f"{node['name']}\n{node['summary']}")

    def get_node(self, node_uuid: str) -> Optional[Dict[str, Any]]:
        row = self._one("SELECT * FROM nodes WHERE uuid=?", (node_uuid,))
        return self._node_out(row) if row else None

    def find_edge(self, conn: sqlite3.Connection, graph_id: str, source: str, target: str, name: str, fact: str) -> Optional[Dict[str, Any]]:
        norm_fact = normalize_text(fact)
        rows = conn.execute(
            "SELECT * FROM edges WHERE graph_id=? AND source_node_uuid=? AND target_node_uuid=? AND name=?",
            (graph_id, source, target, name),
        ).fetchall()
        for row in rows:
            if normalize_text(row["fact"]) == norm_fact:
                return self._edge_out(dict(row))
        return None

    def insert_edge(self, conn: sqlite3.Connection, edge: Dict[str, Any]) -> None:
        cursor = conn.execute(
            "INSERT INTO edges(uuid, graph_id, name, fact, source_node_uuid, target_node_uuid, attributes, "
            "created_at, valid_at, invalid_at, expired_at, episodes, embedding) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                edge["uuid"], edge["graph_id"], edge["name"], edge["fact"],
                edge["source_node_uuid"], edge["target_node_uuid"], _dumps(edge["attributes"]),
                edge["created_at"], edge.get("valid_at"), edge.get("invalid_at"),
                edge.get("expired_at"), _dumps(edge["episodes"]), edge.get("embedding"),
            ),
        )
        self._index(conn, "edges_fts", cursor.lastrowid, edge, edge["fact"])

    def update_edge_episodes(self, conn: sqlite3.Connection, edge_uuid: str, episodes: List[str]) -> None:
        conn.execute("UPDATE edges SET episodes=? WHERE uuid=?", (_dumps(episodes), edge_uuid))

    def get_edge(self, edge_uuid: str) -> Optional[Dict[str, Any]]:
        row = self._one("SELECT * FROM edges WHERE uuid=?", (edge_uuid,))
        return self._edge_out(row) if row else None

    def edges_of_node(self, node_uuid: str) -> List[Dict[str, Any]]:
        rows = self._query(
            "SELECT * FROM edges WHERE source_node_uuid=? OR target_node_uuid=? ORDER BY id",
            (node_uuid, node_uuid),
        )
        return [self._edge_out(row) for row in rows]

    def page(self, kind: str, graph_id: str, limit: Optional[int], cursor: Optional[str]) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """Rows ordered by insertion id; next cursor only when more rows exist."""

        table = {"nodes": "nodes", "edges": "edges"}[kind]
        size = DEFAULT_PAGE_LIMIT if limit is None else int(limit)
        if size < 1:
            raise ValueError("limit must be at least 1")
        after = 0
        if cursor not in (None, ""):
            try:
                after = int(cursor)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid cursor {cursor!r}") from exc
        rows = self._query(
            f"SELECT * FROM {table} WHERE graph_id=? AND id>? ORDER BY id LIMIT ?",
            (graph_id, after, size + 1),
        )
        more = len(rows) > size
        rows = rows[:size]
        next_cursor = str(rows[-1]["id"]) if more and rows else None
        out = self._node_out if kind == "nodes" else self._edge_out
        return [out(row) for row in rows], next_cursor

    @staticmethod
    def _index(conn: sqlite3.Connection, table: str, rowid: Optional[int], row: Dict[str, Any], text: str) -> None:
        conn.execute(f"DELETE FROM {table} WHERE rowid=?", (rowid,))
        conn.execute(
            f"INSERT INTO {table}(rowid, uuid, graph_id, text) VALUES (?,?,?,?)",
            (rowid, row["uuid"], row["graph_id"], normalize_text(text)),
        )

    def lexical_search(self, kind: str, graph_id: str, query: str, limit: int) -> List[str]:
        """Rank uuids by matched query terms.

        Trigram FTS cannot match tokens under 3 characters (most Korean words
        are two syllables), so terms of 3+ characters use a quoted MATCH and
        shorter ones use LIKE on the same FTS text. FTS rowids equal base-table
        ids, so both scans are first restricted to the graph's rowids.
        Rank: distinct query terms hit (desc), then bm25 of the MATCH terms.
        """

        table = {"nodes": "nodes_fts", "edges": "edges_fts"}[kind]
        graph_rows = f"rowid IN (SELECT id FROM {kind} WHERE graph_id=?)"
        terms = query_terms(query)
        if not terms:
            return []
        candidates: Dict[str, Tuple[str, float]] = {}
        long_terms = [term for term in terms if len(term) >= 3]
        short_terms = [term for term in terms if len(term) < 3]
        if long_terms:
            match = " OR ".join('"' + term.replace('"', '""') + '"' for term in long_terms)
            for row in self._query(
                f"SELECT uuid, text, bm25({table}) AS rank FROM {table} "
                f"WHERE {table} MATCH ? AND {graph_rows}",
                (match, graph_id),
            ):
                candidates[row["uuid"]] = (row["text"], row["rank"])
        if short_terms:
            clause = " OR ".join("text LIKE ? ESCAPE '\\'" for _ in short_terms)
            params = [
                "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                for term in short_terms
            ]
            for row in self._query(
                f"SELECT uuid, text FROM {table} WHERE {graph_rows} AND ({clause})",
                (graph_id, *params),
            ):
                candidates.setdefault(row["uuid"], (row["text"], 0.0))
        scored = []
        for row_uuid, (text, bm25) in candidates.items():
            hits = sum(1 for term in terms if term in text)
            if hits:
                scored.append((-hits, bm25, row_uuid))
        scored.sort()
        return [row_uuid for _, _, row_uuid in scored[:limit]]

    def embeddings(self, kind: str, graph_id: str) -> List[Tuple[str, bytes]]:
        table = {"nodes": "nodes", "edges": "edges"}[kind]
        rows = self._query(
            f"SELECT uuid, embedding FROM {table} WHERE graph_id=? AND embedding IS NOT NULL",
            (graph_id,),
        )
        return [(row["uuid"], row["embedding"]) for row in rows]

    def rows_by_uuid(self, kind: str, uuids: Sequence[str]) -> List[Dict[str, Any]]:
        if not uuids:
            return []
        table = {"nodes": "nodes", "edges": "edges"}[kind]
        placeholders = ",".join("?" for _ in uuids)
        rows = {
            row["uuid"]: row
            for row in self._query(f"SELECT * FROM {table} WHERE uuid IN ({placeholders})", tuple(uuids))
        }
        out = self._node_out if kind == "nodes" else self._edge_out
        return [out(rows[u]) for u in uuids if u in rows]

    def create_batch(self, metadata: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        row = {"batch_id": new_uuid(), "metadata": metadata or {}, "status": "draft", "created_at": utc_now()}
        with self._lock:
            self._conn.execute(
                "INSERT INTO batches(batch_id, metadata, status, created_at) VALUES (?,?,?,?)",
                (row["batch_id"], _dumps(row["metadata"]), row["status"], row["created_at"]),
            )
        return row

    def get_batch(self, batch_id: str) -> Optional[Dict[str, Any]]:
        row = self._one("SELECT * FROM batches WHERE batch_id=?", (batch_id,))
        if row:
            row["metadata"] = _loads(row["metadata"], {})
        return row

    def list_batches(self, limit: int, cursor: Optional[int]) -> Tuple[List[Dict[str, Any]], Optional[int]]:
        rows = self._query(
            "SELECT * FROM batches WHERE id>? ORDER BY id LIMIT ?", (int(cursor or 0), limit + 1)
        )
        more = len(rows) > limit
        rows = rows[:limit]
        for row in rows:
            row["metadata"] = _loads(row["metadata"], {})
        return rows, (rows[-1]["id"] if more and rows else None)

    def add_batch_items(self, batch_id: str, graph_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        created = []
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(sequence_index), -1) AS last FROM batch_items WHERE batch_id=?",
                (batch_id,),
            ).fetchone()
            next_index = row["last"] + 1
            for item in graph_items:
                episode = self.add_episode(
                    item["graph_id"], item["data"],
                    source_description=item.get("source_description"),
                    metadata=item.get("metadata"),
                    created_at=item.get("created_at"),
                    locale=item.get("locale"),
                    conn=conn,
                )
                conn.execute(
                    "INSERT INTO batch_items(batch_id, sequence_index, episode_uuid, graph_id, status) "
                    "VALUES (?,?,?,?, 'pending')",
                    (batch_id, next_index, episode["uuid"], item["graph_id"]),
                )
                created.append({
                    "batch_id": batch_id, "sequence_index": next_index,
                    "episode_uuid": episode["uuid"], "graph_id": item["graph_id"],
                    "status": "pending", "error": None,
                })
                next_index += 1
        return created

    def batch_items(self, batch_id: str, limit: Optional[int] = None, cursor: Optional[int] = None) -> Tuple[List[Dict[str, Any]], Optional[int]]:
        if limit is None:
            rows = self._query(
                "SELECT * FROM batch_items WHERE batch_id=? ORDER BY sequence_index", (batch_id,)
            )
            return rows, None
        rows = self._query(
            "SELECT * FROM batch_items WHERE batch_id=? AND sequence_index>? ORDER BY sequence_index LIMIT ?",
            (batch_id, -1 if cursor is None else int(cursor), limit + 1),
        )
        more = len(rows) > limit
        rows = rows[:limit]
        return rows, (rows[-1]["sequence_index"] if more and rows else None)

    def set_batch_status(self, batch_id: str, status: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE batches SET status=? WHERE batch_id=?", (status, batch_id))

    def finish_batch_item(self, episode_uuid: str, status: str, error: Optional[str]) -> List[str]:
        """Record an item outcome; returns batch ids that are now complete."""

        finished = []
        with self.transaction() as conn:
            batch_ids = [
                row["batch_id"]
                for row in conn.execute(
                    "SELECT batch_id FROM batch_items WHERE episode_uuid=?", (episode_uuid,)
                ).fetchall()
            ]
            conn.execute(
                "UPDATE batch_items SET status=?, error=? WHERE episode_uuid=?",
                (status, error, episode_uuid),
            )
            for batch_id in batch_ids:
                counts = self._item_counts(conn, batch_id)
                if counts["pending"] == 0:
                    final = _final_status(counts)
                    conn.execute("UPDATE batches SET status=? WHERE batch_id=?", (final, batch_id))
                    finished.append(batch_id)
        return finished

    @staticmethod
    def _item_counts(conn: sqlite3.Connection, batch_id: str) -> Dict[str, int]:
        counts = {"total": 0, "succeeded": 0, "failed": 0, "pending": 0}
        for row in conn.execute(
            "SELECT status, COUNT(*) AS n FROM batch_items WHERE batch_id=? GROUP BY status", (batch_id,)
        ).fetchall():
            counts["total"] += row["n"]
            key = row["status"] if row["status"] in ("succeeded", "failed") else "pending"
            counts[key] += row["n"]
        return counts

    def batch_counts(self, batch_id: str) -> Dict[str, int]:
        with self._lock:
            return self._item_counts(self._conn, batch_id)


def _final_status(counts: Dict[str, int]) -> str:
    if counts["failed"] == 0:
        return "succeeded"
    if counts["succeeded"] == 0:
        return "failed"
    return "partial"


class _Transaction:
    def __init__(self, store: GraphStore):
        self._store = store

    def __enter__(self) -> sqlite3.Connection:
        self._store._lock.acquire()
        try:
            self._store._conn.execute("BEGIN IMMEDIATE")
        except BaseException:
            self._store._lock.release()
            raise
        return self._store._conn

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self._store._conn.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            self._store._lock.release()
