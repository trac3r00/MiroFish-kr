"""Episode -> entities/edges extraction with one LLM call per episode."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from .. import llm
from ..llm.profiles import STAGES
from ..utils.locale import get_language_instruction
from ..utils.logger import get_logger
from . import embed
from .store import GraphStore, new_uuid, normalize_text

logger = get_logger("mirofish.graph_local.extract")

EXTRACT_STAGE = "graph_extract" if "graph_extract" in STAGES else "default"
DEFAULT_ENTITY_TYPE = "Entity"
DEFAULT_EDGE_TYPE = "RELATED_TO"
EMBEDDING_MERGE_THRESHOLD = 0.92
MAX_SUMMARY_CHARS = 2000
MAX_EPISODE_CHARS = 12000

SYSTEM_PROMPT = (
    "You extract a knowledge graph from one text episode.\n"
    "Return a single JSON object with exactly two keys:\n"
    '{"entities": [{"name": str, "type": str, "summary": str, "attributes": {str: str}}],\n'
    ' "edges": [{"source": str, "target": str, "type": str, "fact": str,'
    ' "valid_at": str|null, "invalid_at": str|null}]}\n'
    "Rules:\n"
    "- Use only the entity types and edge types listed in the user message; "
    "use \"Entity\" when no listed entity type fits.\n"
    "- Keep entity names exactly as written in the text (same script and spelling); "
    "one entry per real-world entity.\n"
    "- Every edge source and target must be the name of an entity in \"entities\".\n"
    "- fact is one self-contained sentence stating the relationship.\n"
    "- valid_at / invalid_at are ISO 8601 timestamps when the text states when the fact "
    "started or stopped being true, otherwise null. Resolve relative dates against the "
    "reference time.\n"
    "- summary is one or two sentences about the entity based only on this text.\n"
    "- Do not invent facts that are not in the text. Output JSON only.\n"
)


class ExtractionFormatError(ValueError):
    pass


@dataclass
class Extraction:
    entities: List[Dict[str, Any]] = field(default_factory=list)
    edges: List[Dict[str, Any]] = field(default_factory=list)
    skipped: int = 0


def system_prompt() -> str:
    return f"{SYSTEM_PROMPT}\n{get_language_instruction()}"


def _ontology_block(ontology: Dict[str, Any]) -> str:
    lines = ["Entity types:"]
    for entity in ontology.get("entity_types", []):
        attrs = ", ".join(a["name"] for a in entity.get("attributes", []))
        lines.append(f"- {entity['name']}: {entity.get('description', '')}" + (f" (attributes: {attrs})" if attrs else ""))
    if len(lines) == 1:
        lines.append("- Entity: any named entity")
    lines.append("Edge types:")
    edge_types = ontology.get("edge_types", [])
    for edge in edge_types:
        pairs = ", ".join(f"{st['source']}->{st['target']}" for st in edge.get("source_targets", []))
        lines.append(f"- {edge['name']}: {edge.get('description', '')}" + (f" ({pairs})" if pairs else ""))
    if not edge_types:
        lines.append(f"- {DEFAULT_EDGE_TYPE}: any relationship")
    return "\n".join(lines)


def build_messages(ontology: Dict[str, Any], text: str, reference_time: str) -> List[Dict[str, str]]:
    user = (
        f"{_ontology_block(ontology)}\n\n"
        f"Reference time: {reference_time}\n\n"
        f"Text:\n{text[:MAX_EPISODE_CHARS]}"
    )
    return [
        {"role": "system", "content": system_prompt()},
        {"role": "user", "content": user},
    ]


def _clean_str(value: Any) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def _clean_attributes(value: Any) -> Dict[str, str]:
    if not isinstance(value, dict):
        return {}
    cleaned = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip():
            continue
        if isinstance(item, (str, int, float)):
            text = str(item).strip()
            if text:
                cleaned[key.strip()] = text
    return cleaned


def _clean_time(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return text


def parse_extraction(payload: Any, ontology: Dict[str, Any]) -> Extraction:
    """Validate LLM output; drops malformed entries, raises on a bad envelope."""

    if not isinstance(payload, dict):
        raise ExtractionFormatError(f"expected a JSON object, got {type(payload).__name__}")
    raw_entities = payload.get("entities", [])
    raw_edges = payload.get("edges", [])
    if not isinstance(raw_entities, list) or not isinstance(raw_edges, list):
        raise ExtractionFormatError("'entities' and 'edges' must be lists")

    entity_types = {e["name"] for e in ontology.get("entity_types", [])}
    edge_types = {e["name"] for e in ontology.get("edge_types", [])}
    result = Extraction()
    by_norm: Dict[str, Dict[str, Any]] = {}

    def add_entity(name: str, entity_type: str, summary: str, attributes: Dict[str, str]) -> None:
        norm = normalize_text(name)
        existing = by_norm.get(norm)
        if existing is None:
            entity = {"name": name, "type": entity_type, "summary": summary, "attributes": attributes}
            by_norm[norm] = entity
            result.entities.append(entity)
            return
        if existing["type"] == DEFAULT_ENTITY_TYPE:
            existing["type"] = entity_type
        existing["summary"] = _merge_summary(existing["summary"], summary)
        existing["attributes"].update(attributes)

    for raw in raw_entities:
        name = _clean_str(raw.get("name")) if isinstance(raw, dict) else ""
        if not name:
            result.skipped += 1
            continue
        entity_type = _clean_str(raw.get("type"))
        add_entity(
            name,
            entity_type if entity_type in entity_types else DEFAULT_ENTITY_TYPE,
            _clean_str(raw.get("summary")),
            _clean_attributes(raw.get("attributes")),
        )

    for raw in raw_edges:
        if not isinstance(raw, dict):
            result.skipped += 1
            continue
        source, target, fact = (_clean_str(raw.get(k)) for k in ("source", "target", "fact"))
        if not source or not target or not fact or normalize_text(source) == normalize_text(target):
            result.skipped += 1
            continue
        for endpoint in (source, target):
            if normalize_text(endpoint) not in by_norm:
                add_entity(endpoint, DEFAULT_ENTITY_TYPE, "", {})
        edge_type = _clean_str(raw.get("type"))
        result.edges.append({
            "source": source,
            "target": target,
            "type": edge_type if edge_type in edge_types else DEFAULT_EDGE_TYPE,
            "fact": fact,
            "valid_at": _clean_time(raw.get("valid_at")),
            "invalid_at": _clean_time(raw.get("invalid_at")),
        })
    return result


def _merge_summary(old: str, new: str) -> str:
    if not new or new in old:
        return old
    if not old or old in new:
        return new[:MAX_SUMMARY_CHARS]
    return f"{old} {new}"[:MAX_SUMMARY_CHARS]


def _labels(entity_type: str, existing: Optional[List[str]] = None) -> List[str]:
    labels = list(existing or ["Entity"])
    if "Entity" not in labels:
        labels.insert(0, "Entity")
    if entity_type != DEFAULT_ENTITY_TYPE and entity_type not in labels:
        labels.append(entity_type)
    return labels


def apply_extraction(
    store: GraphStore,
    graph_id: str,
    episode_uuid: str,
    created_at: str,
    extraction: Extraction,
) -> Tuple[int, int]:
    """Persist entities/edges with entity resolution; returns (nodes, edges) touched.

    Resolution: same graph + normalized name (NFKC, casefold, collapsed
    spaces) is the same node; with embeddings on, a name vector with cosine
    >= EMBEDDING_MERGE_THRESHOLD to an existing node also merges.
    """

    names = [e["name"] for e in extraction.entities]
    name_vectors = embed.embed_passages(names)
    node_vectors = embed.embed_passages(
        [f"{e['name']}: {e['summary']}" for e in extraction.entities]
    )
    edge_vectors = embed.embed_passages([e["fact"] for e in extraction.edges])

    node_uuid_by_norm: Dict[str, str] = {}
    with store.transaction() as conn:
        known_vectors = (
            [(u, embed.from_blob(b)) for u, b in store.node_name_embeddings(conn, graph_id)]
            if name_vectors else []
        )
        for index, entity in enumerate(extraction.entities):
            norm = normalize_text(entity["name"])
            name_vector = name_vectors[index] if name_vectors else None
            node: Optional[Dict[str, Any]] = store.find_node_by_norm(conn, graph_id, norm)
            if node is None and name_vector is not None:
                node = _closest_node(store, conn, known_vectors, name_vector)
            if node is None:
                node = {
                    "uuid": new_uuid(),
                    "graph_id": graph_id,
                    "name": entity["name"],
                    "labels": _labels(entity["type"]),
                    "summary": entity["summary"][:MAX_SUMMARY_CHARS],
                    "attributes": dict(entity["attributes"]),
                    "created_at": created_at,
                    "embedding": embed.to_blob(node_vectors[index]) if node_vectors else None,
                    "name_embedding": embed.to_blob(name_vector),
                }
                store.insert_node(conn, node)
                if name_vector is not None:
                    known_vectors.append((node["uuid"], name_vector))
            else:
                node["labels"] = _labels(entity["type"], node["labels"])
                node["summary"] = _merge_summary(node["summary"], entity["summary"])
                node["attributes"].update(entity["attributes"])
                node["embedding"] = None
                store.update_node(conn, node)
            node_uuid_by_norm[norm] = node["uuid"]

        for index, edge in enumerate(extraction.edges):
            source = node_uuid_by_norm[normalize_text(edge["source"])]
            target = node_uuid_by_norm[normalize_text(edge["target"])]
            if source == target:
                continue
            existing = store.find_edge(conn, graph_id, source, target, edge["type"], edge["fact"])
            if existing is not None:
                if episode_uuid not in existing["episodes"]:
                    store.update_edge_episodes(conn, existing["uuid"], existing["episodes"] + [episode_uuid])
                continue
            store.insert_edge(conn, {
                "uuid": new_uuid(),
                "graph_id": graph_id,
                "name": edge["type"],
                "fact": edge["fact"],
                "source_node_uuid": source,
                "target_node_uuid": target,
                "attributes": {},
                "created_at": created_at,
                "valid_at": edge["valid_at"],
                "invalid_at": edge["invalid_at"],
                "expired_at": None,
                "episodes": [episode_uuid],
                "embedding": embed.to_blob(edge_vectors[index]) if edge_vectors else None,
            })
    return len(set(node_uuid_by_norm.values())), len(extraction.edges)


def _closest_node(store: GraphStore, conn: Any, known: List[Tuple[str, np.ndarray]], vector: np.ndarray) -> Optional[Dict[str, Any]]:
    best_uuid, best_score = None, EMBEDDING_MERGE_THRESHOLD
    for node_uuid, other in known:
        if other.shape != vector.shape:
            continue
        score = float(other @ vector)
        if score >= best_score:
            best_uuid, best_score = node_uuid, score
    if best_uuid is None:
        return None
    row = conn.execute("SELECT * FROM nodes WHERE uuid=?", (best_uuid,)).fetchone()
    return store._node_out(dict(row)) if row else None


def extract_episode(store: GraphStore, episode: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    """Run extraction for one episode; returns (item_status, error).

    Malformed LLM output is not fatal: the episode is processed with zero
    facts and the error is recorded ("succeeded" item). Failures of the LLM
    call itself (auth, transport, unknown stage) mark the item "failed".
    """

    graph_id = episode["graph_id"]
    ontology = store.get_ontology(graph_id)
    messages = build_messages(ontology, episode["data"], episode["created_at"])
    try:
        payload = llm.chat_json(messages, stage=EXTRACT_STAGE, temperature=0.0)
    except (llm.LLMResponseError, json.JSONDecodeError) as error:
        logger.error("graph_local extraction returned malformed JSON for episode %s: %s", episode["uuid"], error)
        return "succeeded", f"malformed extraction output: {error}"
    except Exception as error:
        logger.error("graph_local extraction LLM call failed for episode %s: %s", episode["uuid"], error)
        return "failed", f"{type(error).__name__}: {error}"
    try:
        extraction = parse_extraction(payload, ontology)
    except ExtractionFormatError as error:
        logger.error("graph_local extraction output rejected for episode %s: %s", episode["uuid"], error)
        return "succeeded", f"malformed extraction output: {error}"
    if extraction.skipped:
        logger.warning("graph_local skipped %s malformed entries in episode %s", extraction.skipped, episode["uuid"])
    apply_extraction(store, graph_id, episode["uuid"], episode["created_at"], extraction)
    return "succeeded", None
