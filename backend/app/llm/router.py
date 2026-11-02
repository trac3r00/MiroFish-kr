from __future__ import annotations

import math
import random
from typing import Any, Dict, Iterable, List, Mapping, Optional

from .profiles import STAGES, LLMProfile, LLMSettings, load_settings

DEFAULT_STAGE = "default"


def validate_stage(stage: str) -> None:
    if stage != DEFAULT_STAGE and stage not in STAGES:
        raise ValueError(f"Unknown LLM stage {stage!r}; valid stages: {', '.join(STAGES)}")


def resolve_profile(stage: str, settings: Optional[LLMSettings] = None) -> LLMProfile:
    """Return the profile routed for `stage`; "default" means the default profile."""
    validate_stage(stage)
    settings = settings or load_settings()
    if settings.default_profile_id is None:
        raise ValueError("No LLM profile configured (set LLM_API_KEY or LLM_PROFILES)")
    profile_id = settings.default_profile_id if stage == DEFAULT_STAGE else settings.routes[stage]
    return settings.profile(profile_id)


def _apportion(total: int, weights: Mapping[str, float], rng: random.Random) -> Dict[str, int]:
    """Largest-remainder apportionment: each count is within 1 of total*w/sum(w)."""
    weight_sum = sum(weights.values())
    quotas = {pid: total * w / weight_sum for pid, w in weights.items()}
    counts = {pid: math.floor(q) for pid, q in quotas.items()}
    leftover = total - sum(counts.values())
    tie_break = {pid: rng.random() for pid in weights}
    by_remainder = sorted(weights, key=lambda pid: (-(quotas[pid] - counts[pid]), tie_break[pid]))
    for pid in by_remainder[:leftover]:
        counts[pid] += 1
    return counts


def assign_agent_profiles(
    agent_ids: Iterable[Any],
    seed: Any,
    pool: Optional[Mapping[str, float]] = None,
    settings: Optional[LLMSettings] = None,
) -> Dict[Any, str]:
    """Deterministically map each agent id to a profile id from the weighted agent pool.

    The result depends only on the set of agent ids, the seed, and the pool.
    """
    if pool is None:
        pool = (settings or load_settings()).agent_pool
    if not pool:
        raise ValueError("LLM agent pool is empty (no LLM profile configured)")
    unique_ids: List[Any] = sorted(set(agent_ids), key=lambda a: (type(a).__name__, str(a)))
    rng = random.Random(f"mirofish-agent-pool:{seed}")
    counts = _apportion(len(unique_ids), pool, rng)
    slots = [pid for pid in sorted(counts) for _ in range(counts[pid])]
    rng.shuffle(slots)
    return dict(zip(unique_ids, slots))
