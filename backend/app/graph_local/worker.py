"""Single daemon thread that extracts queued episodes in FIFO order."""

from __future__ import annotations

import queue
import threading
from typing import Any, Callable, Dict, Iterable, Optional, Tuple

from ..utils.locale import set_locale
from ..utils.logger import get_logger
from .extract import extract_episode
from .store import GraphStore

logger = get_logger("mirofish.graph_local.worker")

Processor = Callable[[GraphStore, Dict[str, Any]], Tuple[str, Optional[str]]]


class GraphWorker:
    def __init__(self, store: GraphStore, processor: Processor = extract_episode):
        self.store = store
        self.processor = processor
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._pending = 0
        self._idle = threading.Condition()
        self._thread: Optional[threading.Thread] = None
        self._start_lock = threading.Lock()

    def enqueue(self, episode_uuids: Iterable[str]) -> None:
        uuids = list(episode_uuids)
        if not uuids:
            return
        self.store.mark_episodes_queued(uuids)
        with self._idle:
            self._pending += len(uuids)
        for episode_uuid in uuids:
            self._queue.put(episode_uuid)
        self._ensure_started()

    def resume_pending(self) -> int:
        uuids = self.store.pending_queued_episodes()
        self.enqueue(uuids)
        return len(uuids)

    def wait_idle(self, timeout: float) -> bool:
        with self._idle:
            return self._idle.wait_for(lambda: self._pending == 0, timeout=timeout)

    def _ensure_started(self) -> None:
        with self._start_lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._run, name="graph-local-worker", daemon=True
                )
                self._thread.start()

    def _run(self) -> None:
        while True:
            episode_uuid = self._queue.get()
            try:
                self._handle(episode_uuid)
            except Exception:
                logger.exception("graph_local worker failed on episode %s", episode_uuid)
            finally:
                with self._idle:
                    self._pending -= 1
                    self._idle.notify_all()

    def _handle(self, episode_uuid: str) -> None:
        episode = self.store.get_episode(episode_uuid)
        if episode is None:
            self.store.finish_batch_item(episode_uuid, "failed", "episode graph was deleted")
            return
        if episode["processed"]:
            return
        set_locale(episode.get("locale") or "en")
        try:
            status, error = self.processor(self.store, episode)
        except Exception as exc:
            logger.exception("graph_local extraction crashed for episode %s", episode_uuid)
            status, error = "failed", f"{type(exc).__name__}: {exc}"
        self.store.mark_episode_processed(episode_uuid, error)
        self.store.finish_batch_item(episode_uuid, status, error)
