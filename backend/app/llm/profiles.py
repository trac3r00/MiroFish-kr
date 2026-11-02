"""
LLM profiles: which providers/models exist and where their credentials come from.

Sources (all optional except a legacy key or LLM_PROFILES):
- LLM_API_KEY / LLM_BASE_URL / LLM_MODEL_NAME  -> profile "default"
- LLM_BOOST_API_KEY / _BASE_URL / _MODEL_NAME  -> profile "boost" (only when all three are set)
- LLM_PROFILES  JSON list (inline or path to a .json file); an entry whose id matches a
                legacy profile replaces it in place
- LLM_ROUTES    JSON {stage: profile_id}; unrouted stages use the default profile
- LLM_AGENT_POOL "id1,id2" or JSON {"id": weight}; default = the simulation_agent route

Parsing is pure (`parse_settings(env)`) and raises ValueError with a readable message.
API keys and header values are never part of repr() or public output.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

STAGES: Tuple[str, ...] = (
    "ontology",
    "graph_extract",
    "profile",
    "config",
    "report",
    "report_chat",
    "interview",
    "search_planning",
    "simulation_agent",
    "graph_extract",
)
KINDS = ("openai_compatible", "chatgpt_oauth", "claude_code_cli", "cursor_agent_cli")
DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"
_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../.."))
_LEGACY_KEYS = ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL_NAME")
_ENTRY_FIELDS = {
    "id", "label", "kind", "model", "base_url", "api_key_env",
    "extra_headers", "supports_json_mode",
}


@dataclass(frozen=True)
class LLMProfile:
    id: str
    kind: str
    label: str
    model: str
    base_url: Optional[str]
    api_key: Optional[str] = field(default=None, repr=False, compare=False)
    extra_headers: Dict[str, str] = field(default_factory=dict, repr=False, compare=False, hash=False)
    supports_json_mode: bool = True
    api_key_env: Optional[str] = None

    def to_public(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "kind": self.kind,
            "model": self.model,
            "base_url": _strip_userinfo(self.base_url),
            "supports_json_mode": self.supports_json_mode,
            "has_api_key": bool(self.api_key),
            "api_key_env": self.api_key_env,
            "extra_header_names": sorted(self.extra_headers),
        }


@dataclass(frozen=True)
class LLMSettings:
    profiles: Tuple[LLMProfile, ...]
    routes: Dict[str, str]
    agent_pool: Dict[str, float]
    default_profile_id: Optional[str]

    def profile(self, profile_id: str) -> LLMProfile:
        for profile in self.profiles:
            if profile.id == profile_id:
                return profile
        raise ValueError(f"Unknown LLM profile id: {profile_id!r}")

    def to_public(self) -> Dict[str, Any]:
        return {
            "profiles": [p.to_public() for p in self.profiles],
            "routes": dict(self.routes),
            "agent_pool": dict(self.agent_pool),
            "default_profile_id": self.default_profile_id,
        }


def _strip_userinfo(url: Optional[str]) -> Optional[str]:
    if not url or "@" not in url:
        return url
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def _env_value(env: Mapping[str, str], key: str) -> str:
    value = env.get(key)
    return value.strip() if isinstance(value, str) else ""


def _load_json(raw: str, name: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError(
            f"{name} is not valid JSON (line {error.lineno}, column {error.colno}): {error.msg}"
        ) from None


def _read_profiles_source(raw: str) -> Any:
    text = raw.strip()
    if text.startswith("[") or text.startswith("{"):
        return _load_json(text, "LLM_PROFILES")
    path = os.path.expanduser(text)
    candidates = [path] if os.path.isabs(path) else [path, os.path.join(_PROJECT_ROOT, path)]
    for candidate in candidates:
        if os.path.isfile(candidate):
            with open(candidate, "r", encoding="utf-8") as handle:
                return _load_json(handle.read(), f"LLM_PROFILES file {text!r}")
    raise ValueError(
        f"LLM_PROFILES must be a JSON list or a path to a .json file; file not found: {text!r}"
    )


def _parse_profile_entry(entry: Any, index: int, env: Mapping[str, str]) -> LLMProfile:
    where = f"LLM_PROFILES[{index}]"
    if not isinstance(entry, dict):
        raise ValueError(f"{where} must be a JSON object")
    unknown = sorted(set(entry) - _ENTRY_FIELDS)
    if unknown:
        raise ValueError(f"{where} has unknown field(s): {', '.join(unknown)}")

    profile_id = entry.get("id")
    if not isinstance(profile_id, str) or not _ID_RE.match(profile_id):
        raise ValueError(f"{where}.id must be 1-64 chars of letters, digits, '_', '-', '.'")
    where = f"LLM_PROFILES[{index}] ({profile_id})"

    kind = entry.get("kind", "openai_compatible")
    if kind not in KINDS:
        raise ValueError(f"{where}.kind must be one of {', '.join(KINDS)}")

    model = entry.get("model")
    if kind in ("claude_code_cli", "cursor_agent_cli") and model is None:
        model = ""
    elif not isinstance(model, str) or not model.strip():
        raise ValueError(f"{where}.model must be a non-empty string")

    label = entry.get("label", profile_id)
    if not isinstance(label, str) or not label.strip():
        raise ValueError(f"{where}.label must be a non-empty string")

    base_url = entry.get("base_url")
    if base_url is not None and (
        not isinstance(base_url, str) or urlsplit(base_url).scheme not in ("http", "https")
        or not urlsplit(base_url).hostname
    ):
        raise ValueError(f"{where}.base_url must be an http(s) URL")

    api_key_env = entry.get("api_key_env")
    api_key: Optional[str] = None
    if api_key_env is not None:
        if not isinstance(api_key_env, str) or not api_key_env.strip():
            raise ValueError(f"{where}.api_key_env must be an environment variable name")
        api_key = _env_value(env, api_key_env) or None

    if kind == "openai_compatible":
        if base_url is None:
            raise ValueError(f"{where}.base_url is required for kind openai_compatible")
        if api_key_env is None:
            raise ValueError(f"{where}.api_key_env is required for kind openai_compatible")
        if api_key is None:
            raise ValueError(f"{where}: environment variable {api_key_env} is not set")

    headers = entry.get("extra_headers", {}) or {}
    if not isinstance(headers, dict) or not all(
        isinstance(k, str) and k and isinstance(v, str) for k, v in headers.items()
    ):
        raise ValueError(f"{where}.extra_headers must be an object of string -> string")

    json_mode = entry.get("supports_json_mode", True)
    if not isinstance(json_mode, bool):
        raise ValueError(f"{where}.supports_json_mode must be true or false")

    return LLMProfile(
        id=profile_id,
        kind=kind,
        label=label.strip(),
        model=model.strip(),
        base_url=base_url,
        api_key=api_key,
        extra_headers=dict(headers),
        supports_json_mode=json_mode,
        api_key_env=api_key_env,
    )


def _legacy_profiles(env: Mapping[str, str]) -> List[LLMProfile]:
    profiles: List[LLMProfile] = []
    api_key = _env_value(env, "LLM_API_KEY")
    if api_key:
        profiles.append(LLMProfile(
            id="default",
            kind="openai_compatible",
            label="Default",
            model=_env_value(env, "LLM_MODEL_NAME") or DEFAULT_MODEL,
            base_url=_env_value(env, "LLM_BASE_URL") or DEFAULT_BASE_URL,
            api_key=api_key,
            api_key_env="LLM_API_KEY",
        ))
    boost = [_env_value(env, f"LLM_BOOST_{suffix}") for suffix in ("API_KEY", "BASE_URL", "MODEL_NAME")]
    if all(boost):
        profiles.append(LLMProfile(
            id="boost",
            kind="openai_compatible",
            label="Boost",
            model=boost[2],
            base_url=boost[1],
            api_key=boost[0],
            api_key_env="LLM_BOOST_API_KEY",
        ))
    return profiles


def _parse_agent_pool(raw: str, known: Dict[str, LLMProfile]) -> Dict[str, float]:
    text = raw.strip()
    pool: Dict[str, float] = {}
    if text.startswith("{"):
        data = _load_json(text, "LLM_AGENT_POOL")
        if not isinstance(data, dict) or not data:
            raise ValueError("LLM_AGENT_POOL JSON must be a non-empty object {profile_id: weight}")
        for profile_id, weight in data.items():
            if isinstance(weight, bool) or not isinstance(weight, (int, float)) or not weight > 0:
                raise ValueError(f"LLM_AGENT_POOL weight for {profile_id!r} must be a positive number")
            if weight == float("inf"):
                raise ValueError(f"LLM_AGENT_POOL weight for {profile_id!r} must be finite")
            pool[profile_id] = float(weight)
    else:
        for item in text.split(","):
            profile_id = item.strip()
            if not profile_id:
                raise ValueError("LLM_AGENT_POOL contains an empty profile id")
            if profile_id in pool:
                raise ValueError(f"LLM_AGENT_POOL lists {profile_id!r} more than once")
            pool[profile_id] = 1.0
    for profile_id in pool:
        if profile_id not in known:
            raise ValueError(f"LLM_AGENT_POOL references unknown profile id {profile_id!r}")
    return pool


def parse_settings(env: Mapping[str, str]) -> LLMSettings:
    """Build settings from an environment mapping. Raises ValueError on bad config."""
    profiles = _legacy_profiles(env)

    raw_profiles = _env_value(env, "LLM_PROFILES")
    if raw_profiles:
        entries = _read_profiles_source(raw_profiles)
        if not isinstance(entries, list):
            raise ValueError("LLM_PROFILES must be a JSON list of profile objects")
        seen: set[str] = set()
        for index, entry in enumerate(entries):
            profile = _parse_profile_entry(entry, index, env)
            if profile.id in seen:
                raise ValueError(f"LLM_PROFILES defines profile id {profile.id!r} more than once")
            seen.add(profile.id)
            positions = [i for i, p in enumerate(profiles) if p.id == profile.id]
            if positions:
                profiles[positions[0]] = profile
            else:
                profiles.append(profile)

    known = {p.id: p for p in profiles}
    default_id = "default" if "default" in known else (profiles[0].id if profiles else None)

    routes: Dict[str, str] = {stage: default_id for stage in STAGES} if default_id else {}
    raw_routes = _env_value(env, "LLM_ROUTES")
    if raw_routes:
        data = _load_json(raw_routes, "LLM_ROUTES")
        if not isinstance(data, dict):
            raise ValueError("LLM_ROUTES must be a JSON object {stage: profile_id}")
        for stage, profile_id in data.items():
            if stage not in STAGES:
                raise ValueError(
                    f"LLM_ROUTES has unknown stage {stage!r}; valid stages: {', '.join(STAGES)}"
                )
            if not isinstance(profile_id, str) or profile_id not in known:
                raise ValueError(f"LLM_ROUTES[{stage!r}] references unknown profile id {profile_id!r}")
            routes[stage] = profile_id

    raw_pool = _env_value(env, "LLM_AGENT_POOL")
    if raw_pool:
        agent_pool = _parse_agent_pool(raw_pool, known)
    elif routes:
        agent_pool = {routes["simulation_agent"]: 1.0}
    else:
        agent_pool = {}

    return LLMSettings(
        profiles=tuple(profiles),
        routes=routes,
        agent_pool=agent_pool,
        default_profile_id=default_id,
    )


def _current_env() -> Dict[str, str]:
    """os.environ, with legacy LLM_* keys falling back to Config attributes."""
    env = dict(os.environ)
    from ..config import Config

    for key in _LEGACY_KEYS:
        if not _env_value(env, key):
            value = getattr(Config, key, None)
            if value:
                env[key] = value
    return env


def load_settings(env: Optional[Mapping[str, str]] = None) -> LLMSettings:
    return parse_settings(_current_env() if env is None else env)


def load_profiles(env: Optional[Mapping[str, str]] = None) -> List[LLMProfile]:
    return list(load_settings(env).profiles)


def get_profile(profile_id: str, env: Optional[Mapping[str, str]] = None) -> LLMProfile:
    return load_settings(env).profile(profile_id)


def list_profiles_public(env: Optional[Mapping[str, str]] = None) -> List[Dict[str, Any]]:
    return [p.to_public() for p in load_settings(env).profiles]
