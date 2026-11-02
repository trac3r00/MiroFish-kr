from __future__ import annotations

import threading
from typing import Any, Dict, Tuple

_FIELDS = ("calls", "input_tokens", "cached_tokens", "output_tokens")


class UsageTracker:
    """Thread-safe in-memory token totals keyed by (stage, profile_id, model)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._totals: Dict[Tuple[str, str, str], Dict[str, int]] = {}

    def record(
        self,
        stage: str,
        profile_id: str,
        model: str,
        input_tokens: int = 0,
        cached_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        key = (str(stage), str(profile_id), str(model))
        with self._lock:
            entry = self._totals.setdefault(key, dict.fromkeys(_FIELDS, 0))
            entry["calls"] += 1
            entry["input_tokens"] += int(input_tokens or 0)
            entry["cached_tokens"] += int(cached_tokens or 0)
            entry["output_tokens"] += int(output_tokens or 0)

    def snapshot(self) -> Dict[str, Any]:
        """{"entries": [{stage, profile_id, model, calls, input/cached/output_tokens}], "totals": {...}}"""
        with self._lock:
            items = [(key, dict(value)) for key, value in self._totals.items()]
        items.sort(key=lambda item: item[0])
        totals = dict.fromkeys(_FIELDS, 0)
        entries = []
        for (stage, profile_id, model), counts in items:
            entries.append({"stage": stage, "profile_id": profile_id, "model": model, **counts})
            for name in _FIELDS:
                totals[name] += counts[name]
        return {"entries": entries, "totals": totals}

    def reset(self) -> None:
        with self._lock:
            self._totals.clear()


def _field(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    value = getattr(obj, name, None)
    if value is None:
        extra = getattr(obj, "model_extra", None)
        if isinstance(extra, dict):
            value = extra.get(name)
    return value


def usage_from_chat_completion(response: Any) -> Dict[str, int]:
    """Normalize Chat Completions usage (OpenAI cached_tokens, DeepSeek prompt_cache_hit_tokens)."""
    usage = _field(response, "usage")
    cached = _field(_field(usage, "prompt_tokens_details"), "cached_tokens")
    if cached is None:
        cached = _field(usage, "prompt_cache_hit_tokens")
    return {
        "input_tokens": int(_field(usage, "prompt_tokens") or 0),
        "cached_tokens": int(cached or 0),
        "output_tokens": int(_field(usage, "completion_tokens") or 0),
    }


usage_tracker = UsageTracker()
