"""Localized persona archetypes and deterministic agent assignment."""

import json
import random
from functools import lru_cache
from pathlib import Path
from typing import Any


_DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "archetypes"
_LOCALES = ("ko", "en", "zh")
_PRESET_LOCALES = {
    "ko_everyday": "ko",
    "en_everyday": "en",
    "zh_everyday": "zh",
}


@lru_cache(maxsize=len(_LOCALES))
def _load_archetypes(locale: str) -> tuple[dict[str, Any], ...]:
    with (_DATA_DIR / f"{locale}.json").open(encoding="utf-8") as handle:
        return tuple(json.load(handle))


def _preset_archetypes(preset: str) -> tuple[dict[str, Any], ...]:
    if preset in _PRESET_LOCALES:
        return _load_archetypes(_PRESET_LOCALES[preset])
    if preset == "mixed_global":
        return tuple(
            {**archetype, "id": f"{locale}:{archetype['id']}"}
            for locale in _LOCALES
            for archetype in _load_archetypes(locale)
        )
    if preset == "off":
        return ()
    raise ValueError(f"Unknown persona archetype preset: {preset}")


def list_presets(locale: str | None = None) -> list[dict[str, Any]]:
    """List available presets, optionally limiting locale-specific presets."""
    presets: list[dict[str, Any]] = [
        {
            "id": preset,
            "label": preset.removesuffix("_everyday").upper() + " everyday",
            "locale": preset.removesuffix("_everyday"),
            "archetype_count": len(_load_archetypes(preset.removesuffix("_everyday"))),
        }
        for preset in _PRESET_LOCALES
        if locale is None or preset.startswith(f"{locale}_")
    ]
    if locale is None:
        presets.extend(
            [
                {
                    "id": "mixed_global",
                    "label": "Mixed global",
                    "locale": None,
                    "archetype_count": len(_preset_archetypes("mixed_global")),
                },
                {"id": "off", "label": "Off", "locale": None, "archetype_count": 0},
            ]
        )
    return presets


def get_archetype(archetype_id: str) -> dict[str, Any]:
    """Return a copy of an archetype by its id across all localized catalogs."""
    if ":" in archetype_id:
        locale, local_id = archetype_id.split(":", 1)
        if locale in _LOCALES:
            for archetype in _load_archetypes(locale):
                if archetype["id"] == local_id:
                    return dict(archetype)
        raise KeyError(archetype_id)
    for locale in _LOCALES:
        for archetype in _load_archetypes(locale):
            if archetype["id"] == archetype_id:
                return dict(archetype)
    raise KeyError(archetype_id)


def assign_archetypes(agent_ids: list[Any], preset: str, seed: int) -> dict[Any, str | None]:
    """Assign a deterministic, largest-remainder weighted mix to agents."""
    archetypes = _preset_archetypes(preset)
    if preset == "off":
        return dict.fromkeys(agent_ids)
    if not archetypes:
        return dict.fromkeys(agent_ids)

    total_weight = sum(archetype["weight"] for archetype in archetypes)
    exact_counts = [
        len(agent_ids) * archetype["weight"] / total_weight
        for archetype in archetypes
    ]
    counts = [int(count) for count in exact_counts]
    remainder = len(agent_ids) - sum(counts)
    order = sorted(
        range(len(archetypes)),
        key=lambda index: (-(exact_counts[index] - counts[index]), index),
    )
    for index in order[:remainder]:
        counts[index] += 1

    assignments: list[str] = [
        archetype["id"]
        for archetype, count in zip(archetypes, counts)
        for _ in range(count)
    ]
    random.Random(seed).shuffle(assignments)
    return dict(zip(agent_ids, assignments))


def archetype_prompt_block(archetype_id: str, output_locale: str) -> str:
    """Build a short English persona instruction with a requested output locale."""
    archetype = get_archetype(archetype_id)
    return (
        "Persona archetype: "
        f"{archetype['traits_en']} "
        f"Posting style: {archetype['posting_style_en']} "
        f"Write persona text in {output_locale}."
    )


def archetype_activity_overrides(archetype_id: str) -> dict[str, Any]:
    """Return simulation behavior parameters for one archetype."""
    archetype = get_archetype(archetype_id)
    return {
        key: archetype[key]
        for key in (
            "activity_level",
            "active_hours",
            "response_delay_minutes",
            "sentiment_bias",
            "influence_weight",
        )
    }
