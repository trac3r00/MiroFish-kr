"""Free local graph backend emulating the Zep Cloud surface MiroFish uses."""

from __future__ import annotations

import threading
from typing import Any, Dict, Optional

from . import embed
from .client import LocalZepClient
from .store import GraphStore, default_db_path

__all__ = ["LocalZepClient", "GraphStore", "get_local_zep_client", "status"]

_clients: Dict[str, LocalZepClient] = {}
_clients_lock = threading.Lock()


def get_local_zep_client(db_path: Optional[str] = None) -> LocalZepClient:
    path = db_path or default_db_path()
    with _clients_lock:
        client = _clients.get(path)
        if client is None:
            client = LocalZepClient(db_path=path)
            _clients[path] = client
        return client


def status() -> Dict[str, Any]:
    return {"backend": "local", "db_path": default_db_path(), "embeddings": embed.status()}
