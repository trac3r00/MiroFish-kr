"""Optional local embeddings (multilingual-e5-small) for graph search.

Loaded lazily on first use. When sentence-transformers is not importable, the
model cannot load, or ``GRAPH_LOCAL_EMBEDDINGS=off``, the backend runs
FTS-only and ``status()`` says why.
"""

from __future__ import annotations

import importlib.util
import os
import threading
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from ..utils.logger import get_logger

logger = get_logger("mirofish.graph_local.embed")

MODEL_NAME = "intfloat/multilingual-e5-small"

_lock = threading.Lock()
_model: Any = None
_failure: Optional[str] = None
_override: Any = None


def _disabled_reason() -> Optional[str]:
    if os.environ.get("GRAPH_LOCAL_EMBEDDINGS", "").strip().lower() == "off":
        return "disabled by GRAPH_LOCAL_EMBEDDINGS=off"
    if _override is not None:
        return None
    if importlib.util.find_spec("sentence_transformers") is None:
        return "sentence-transformers is not installed"
    return _failure


def status() -> Dict[str, Any]:
    reason = _disabled_reason()
    return {
        "enabled": reason is None,
        "model": getattr(_override, "name", MODEL_NAME),
        "loaded": _override is not None or _model is not None,
        "mode": "fts+embeddings" if reason is None else "fts-only",
        "reason": reason,
    }


def set_encoder(encoder: Any) -> None:
    """Install a test/alternate encoder with ``encode(texts) -> 2D array``."""

    global _override
    _override = encoder


def _encoder() -> Any:
    global _model, _failure
    if _override is not None:
        return _override
    with _lock:
        if _model is None and _failure is None:
            try:
                from sentence_transformers import SentenceTransformer

                _model = SentenceTransformer(MODEL_NAME, device="cpu")
                logger.info("Loaded local embedding model %s", MODEL_NAME)
            except Exception as error:
                _failure = f"model load failed: {type(error).__name__}: {error}"
                logger.warning("Local embeddings unavailable, using FTS only (%s)", _failure)
        return _model


def _encode(texts: Sequence[str]) -> Optional[List[np.ndarray]]:
    if not texts or _disabled_reason() is not None:
        return None
    encoder = _encoder()
    if encoder is None:
        return None
    try:
        vectors = np.asarray(
            encoder.encode(list(texts), normalize_embeddings=True), dtype=np.float32
        )
    except Exception as error:
        logger.warning("Embedding failed, continuing without vectors: %s", error)
        return None
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    vectors = vectors / np.where(norms == 0, 1.0, norms)
    return list(vectors)


def embed_passages(texts: Sequence[str]) -> Optional[List[np.ndarray]]:
    return _encode([f"passage: {text}" for text in texts])


def embed_query(text: str) -> Optional[np.ndarray]:
    vectors = _encode([f"query: {text}"])
    return vectors[0] if vectors else None


def to_blob(vector: Optional[np.ndarray]) -> Optional[bytes]:
    return None if vector is None else np.asarray(vector, dtype=np.float32).tobytes()


def from_blob(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


def rank_by_cosine(query: np.ndarray, rows: Sequence[Tuple[str, bytes]], limit: int) -> List[Tuple[str, float]]:
    if not rows:
        return []
    matrix = np.stack([from_blob(blob) for _, blob in rows])
    if matrix.shape[1] != query.shape[0]:
        return []
    scores = matrix @ query
    order = np.argsort(-scores)[:limit]
    return [(rows[i][0], float(scores[i])) for i in order]
