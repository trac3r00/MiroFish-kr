"""
Sign in with ChatGPT (OpenAI's open-source token-sharing flow) for `chatgpt_oauth` profiles.

Flow (https://developers.openai.com/siwc/token-sharing-open-source/sign-in):
- start_sign_in() binds a one-shot loopback listener on http://127.0.0.1:<port>/auth/callback
  (1455 preferred) BEFORE returning the authorize URL. First registration uses
  client_id=dynamic_agent_client + agent_name_hint; returning sign-in uses the saved issued
  client id + id_token_hint. Fresh state, nonce and PKCE S256 verifier per attempt.
- The callback validates state, handles OAuth errors, exchanges the code, validates the ID token
  (JWKS signature + iss/aud/exp/nonce), records the granted scopes and saves one 0600 JSON record
  per issued client id (atomic replace).
- get_access_token() refreshes under a lock when less than 5 minutes remain and replaces access,
  refresh, expiry and scopes together.

Tokens never leave this module except through get_access_token(); get_status() is token-free.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import html
import json
import logging
import os
import re
import secrets
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urlsplit

import requests

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

ISSUER = "https://auth.openai.com"
AUTHORIZE_URL = "https://auth.openai.com/api/accounts/authorize"
TOKEN_URL = "https://auth.openai.com/api/accounts/oauth/token"
OPENID_CONFIGURATION_URL = "https://auth.openai.com/.well-known/openid-configuration"
MODELS_URL = "https://api.openai.com/v1/models"
RESOURCE = "https://api.openai.com/v1"
SCOPE = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
DYNAMIC_CLIENT_ID = "dynamic_agent_client"
AGENT_NAME = "MiroFish"
CALLBACK_PATH = "/auth/callback"
PREFERRED_PORT = 1455
REFRESH_MARGIN_SECONDS = 300
ATTEMPT_TIMEOUT_SECONDS = 600
MODELS_CACHE_SECONDS = 600
HTTP_TIMEOUT = 30
ID_TOKEN_LEEWAY_SECONDS = 60

TERMINAL_REFRESH_ERRORS = {
    "invalid_grant", "invalid_refresh_token", "token_expired", "refresh_token_expired",
    "refresh_token_invalidated", "refresh_token_reused", "invalid_client",
}

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,128}$")
_store_lock = threading.RLock()
_attempts_lock = threading.Lock()
_attempts: Dict[str, "_Attempt"] = {}
_current_attempt_id: Optional[str] = None
_models_cache: Dict[str, Tuple[float, List[Dict[str, str]]]] = {}
_oidc_cache: Dict[str, Any] = {}


class OAuthError(Exception):
    """Sign-in / token failure; `code` is machine-readable and never contains token values."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code



def credential_dir() -> str:
    path = os.environ.get("MIROFISH_CREDENTIAL_DIR") or os.path.join(
        os.path.expanduser("~"), ".config", "mirofish", "chatgpt"
    )
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def _atomic_write_json(path: str, data: Dict[str, Any]) -> None:
    directory = os.path.dirname(path)
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=directory)
    try:
        os.chmod(tmp_path, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        os.chmod(path, 0o600)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_path)
        raise


def _read_json(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return None
    return data if isinstance(data, dict) else None


def _account_path(client_id: str) -> str:
    name = client_id if _SAFE_ID_RE.match(client_id) else hashlib.sha256(client_id.encode()).hexdigest()
    return os.path.join(credential_dir(), f"account-{name}.json")


def _active_path() -> str:
    return os.path.join(credential_dir(), "active.json")


def get_host_id() -> str:
    """Stable per-host `ext_agent_host_id`, created once and persisted in the credential dir."""
    with _store_lock:
        path = os.path.join(credential_dir(), "host.json")
        data = _read_json(path) or {}
        host_id = data.get("ext_agent_host_id")
        if not isinstance(host_id, str) or not host_id.startswith("urn:uuid:"):
            host_id = f"urn:uuid:{uuid.uuid4()}"
            _atomic_write_json(path, {"ext_agent_host_id": host_id})
        return host_id


def _active_client_id() -> Optional[str]:
    data = _read_json(_active_path()) or {}
    client_id = data.get("client_id")
    return client_id if isinstance(client_id, str) and client_id else None


def _load_active_record() -> Optional[Dict[str, Any]]:
    client_id = _active_client_id()
    return _read_json(_account_path(client_id)) if client_id else None


def _save_record(record: Dict[str, Any], *, make_active: bool) -> None:
    client_id = record["client_id"]
    if not client_id or client_id == DYNAMIC_CLIENT_ID:
        raise OAuthError("registration_incomplete", "refusing to save a non-issued client id")
    with _store_lock:
        _atomic_write_json(_account_path(client_id), record)
        if make_active:
            _atomic_write_json(_active_path(), {"client_id": client_id})


def _now() -> float:
    return time.time()


def _scopes_from(value: Any, fallback: Optional[List[str]] = None) -> List[str]:
    if isinstance(value, str) and value.strip():
        return sorted(set(value.split()))
    return list(fallback or [])


def _apply_token_response(record: Dict[str, Any], token: Dict[str, Any]) -> None:
    expires_in = int(token.get("expires_in") or 3600)
    received = _now()
    record["access_token"] = token["access_token"]
    if token.get("refresh_token"):
        record["refresh_token"] = token["refresh_token"]
    if token.get("id_token"):
        record["id_token"] = token["id_token"]
    record["token_type"] = token.get("token_type") or "Bearer"
    record["expires_in"] = expires_in
    record["expires_at"] = int(received + expires_in)
    if token.get("earliest_refresh_at") is not None:
        record["earliest_refresh_at"] = token.get("earliest_refresh_at")
    record["scopes"] = _scopes_from(token.get("scope"), record.get("scopes"))
    record["saved_at"] = datetime.fromtimestamp(received, timezone.utc).isoformat()



def _post_form(url: str, data: Dict[str, str]) -> Tuple[int, Any]:
    response = requests.post(
        url, data=data, headers={"Accept": "application/json"}, timeout=HTTP_TIMEOUT
    )
    try:
        body = response.json() if response.content else None
    except ValueError:
        body = None
    return response.status_code, body


def _oauth_error_code(body: Any, status: int) -> str:
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, str) and error:
            return error
        if isinstance(error, dict) and error.get("code"):
            return str(error["code"])
    return f"http_{status}"


def _openid_configuration() -> Dict[str, Any]:
    cached = _oidc_cache.get(OPENID_CONFIGURATION_URL)
    if cached is not None:
        return cached
    response = requests.get(OPENID_CONFIGURATION_URL, timeout=HTTP_TIMEOUT)
    response.raise_for_status()
    data = response.json()
    _oidc_cache[OPENID_CONFIGURATION_URL] = data
    return data


def _validate_id_token(id_token: str, *, client_id: str, nonce: Optional[str]) -> Dict[str, Any]:
    """Verify signature against the published JWKS and check iss/aud/exp/nonce."""
    import jwt

    try:
        jwks_uri = _openid_configuration()["jwks_uri"]
        signing_key = jwt.PyJWKClient(jwks_uri).get_signing_key_from_jwt(id_token)
        algorithm = getattr(signing_key, "algorithm_name", None) or "RS256"
        claims = jwt.decode(
            id_token,
            signing_key.key,
            algorithms=[algorithm],
            audience=client_id,
            issuer=ISSUER,
            leeway=ID_TOKEN_LEEWAY_SECONDS,
            options={"require": ["exp", "iss", "aud", "sub"]},
        )
    except (jwt.PyJWTError, KeyError, requests.RequestException) as error:
        raise OAuthError("invalid_id_token", type(error).__name__) from None
    if nonce is not None and not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
        raise OAuthError("invalid_id_token", "nonce mismatch")
    return claims



@dataclass
class _Attempt:
    attempt_id: str
    client_id: str
    state: str
    nonce: str
    code_verifier: str
    redirect_uri: str
    expected_subject: Optional[str]
    server: HTTPServer
    status: str = "pending"
    error: Optional[str] = None
    plan_usage_enabled: Optional[bool] = None
    cancelled: threading.Event = field(default_factory=threading.Event)
    finished: threading.Event = field(default_factory=threading.Event)
    thread: Optional[threading.Thread] = None

    @property
    def is_new_registration(self) -> bool:
        return self.client_id == DYNAMIC_CLIENT_ID

    def public(self) -> Dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "status": self.status,
            "error": self.error,
            "plan_usage_enabled": self.plan_usage_enabled,
        }


def _pkce_pair() -> Tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


_PAGE = (
    "<!doctype html><html><head><meta charset='utf-8'><title>MiroFish</title></head>"
    "<body style='font-family:sans-serif;text-align:center;margin-top:15vh'>"
    "<h2>{title}</h2><p>{detail}</p></body></html>"
)


def _make_handler(attempt: _Attempt):
    class CallbackHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - never log query strings
            return

        def _send(self, status: int, title: str, detail: str = "") -> None:
            body = _PAGE.format(title=html.escape(title), detail=html.escape(detail)).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            parts = urlsplit(self.path)
            if parts.path != CALLBACK_PATH:
                self._send(404, "Not found")
                return
            params = {k: v[0] for k, v in parse_qs(parts.query).items() if v}
            ok, code = _handle_callback(attempt, params)
            if ok:
                self._send(200, "Signed in - you can close this tab")
            elif code == "state_mismatch":
                self._send(400, "Sign-in rejected", "state mismatch")
            else:
                self._send(400, "Sign-in failed", code)

    return CallbackHandler


def _finish(attempt: _Attempt, status: str, error: Optional[str] = None) -> None:
    attempt.status = status
    attempt.error = error
    attempt.finished.set()


def _handle_callback(attempt: _Attempt, params: Dict[str, str]) -> Tuple[bool, str]:
    if attempt.finished.is_set():
        return False, attempt.error or "attempt_closed"
    state = params.get("state", "")
    if not secrets.compare_digest(state, attempt.state):
        # A forged/stale callback must not end the real attempt; keep listening.
        return False, "state_mismatch"

    if params.get("error"):
        code = params["error"] if re.match(r"^[a-z_]{1,64}$", params["error"]) else "oauth_error"
        _finish(attempt, "error", code)
        return False, code

    returned_client = params.get("client_id")
    if attempt.is_new_registration:
        if not returned_client or returned_client == DYNAMIC_CLIENT_ID:
            _finish(attempt, "error", "registration_incomplete")
            return False, "registration_incomplete"
        issued_client = returned_client
    else:
        if returned_client and returned_client != attempt.client_id:
            _finish(attempt, "error", "client_mismatch")
            return False, "client_mismatch"
        issued_client = attempt.client_id

    code = params.get("code")
    if not code:
        _finish(attempt, "error", "missing_code")
        return False, "missing_code"

    try:
        status, token = _post_form(TOKEN_URL, {
            "grant_type": "authorization_code",
            "client_id": issued_client,
            "code": code,
            "code_verifier": attempt.code_verifier,
            "redirect_uri": attempt.redirect_uri,
            "resource": RESOURCE,
        })
    except requests.RequestException:
        _finish(attempt, "error", "token_exchange_failed")
        return False, "token_exchange_failed"
    if status != 200 or not isinstance(token, dict) or not token.get("access_token"):
        error_code = _oauth_error_code(token, status)
        _finish(attempt, "error", error_code)
        return False, error_code
    if not token.get("id_token"):
        _finish(attempt, "error", "invalid_id_token")
        return False, "invalid_id_token"

    try:
        claims = _validate_id_token(token["id_token"], client_id=issued_client, nonce=attempt.nonce)
    except OAuthError as error:
        _finish(attempt, "error", error.code)
        return False, error.code

    subject = str(claims["sub"])
    if attempt.expected_subject and subject != attempt.expected_subject:
        _finish(attempt, "error", "account_mismatch")
        return False, "account_mismatch"

    with _store_lock:
        record = _read_json(_account_path(issued_client)) or {}
        record.update({
            "email": claims.get("email") or record.get("email"),
            "issuer": ISSUER,
            "subject": subject,
            "client_id": issued_client,
            "ext_agent_host_id": get_host_id(),
        })
        _apply_token_response(record, token)
        _save_record(record, make_active=True)
        _models_cache.pop(issued_client, None)
    attempt.plan_usage_enabled = PLAN_SCOPE in record["scopes"]
    _finish(attempt, "success")
    return True, ""


def _serve(attempt: _Attempt, deadline: float) -> None:
    server = attempt.server
    server.timeout = 0.25
    try:
        while not attempt.finished.is_set() and not attempt.cancelled.is_set() and _now() < deadline:
            server.handle_request()
    finally:
        server.server_close()
        if not attempt.finished.is_set():
            _finish(attempt, "cancelled" if attempt.cancelled.is_set() else "error",
                    None if attempt.cancelled.is_set() else "timeout")


def _bind_listener() -> HTTPServer:
    for port in (PREFERRED_PORT, 0):
        try:
            return HTTPServer(("127.0.0.1", port), BaseHTTPRequestHandler)
        except OSError:
            if port == 0:
                raise
    raise OSError("no loopback port available")  # pragma: no cover


def start_sign_in(new_account: bool = False) -> Dict[str, str]:
    """Bind the loopback listener, then return {"authorize_url", "attempt_id"}."""
    global _current_attempt_id
    cancel_pending_sign_in()

    record: Dict[str, Any] = {} if new_account else (_load_active_record() or {})
    saved_client = str(record.get("client_id") or "")
    returning = bool(saved_client) and saved_client != DYNAMIC_CLIENT_ID

    server = _bind_listener()
    port = server.server_address[1]
    redirect_uri = f"http://127.0.0.1:{port}{CALLBACK_PATH}"
    verifier, challenge = _pkce_pair()
    attempt = _Attempt(
        attempt_id=secrets.token_urlsafe(12),
        client_id=saved_client if returning else DYNAMIC_CLIENT_ID,
        state=secrets.token_urlsafe(32),
        nonce=secrets.token_urlsafe(32),
        code_verifier=verifier,
        redirect_uri=redirect_uri,
        expected_subject=record.get("subject") if returning else None,
        server=server,
    )
    server.RequestHandlerClass = _make_handler(attempt)

    params: Dict[str, str] = {
        "client_id": attempt.client_id,
        "ext_agent_host_id": get_host_id(),
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": SCOPE,
        "resource": RESOURCE,
        "state": attempt.state,
        "nonce": attempt.nonce,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
    }
    if returning:
        if record.get("id_token"):
            params["id_token_hint"] = record["id_token"]
        if record.get("email"):
            params["login_hint"] = record["email"]
    else:
        params["agent_name_hint"] = AGENT_NAME

    attempt.thread = threading.Thread(
        target=_serve, args=(attempt, _now() + ATTEMPT_TIMEOUT_SECONDS),
        name="chatgpt-oauth-callback", daemon=True,
    )
    with _attempts_lock:
        _attempts[attempt.attempt_id] = attempt
        _current_attempt_id = attempt.attempt_id
    attempt.thread.start()
    return {"authorize_url": f"{AUTHORIZE_URL}?{urlencode(params)}", "attempt_id": attempt.attempt_id}


def cancel_pending_sign_in() -> None:
    with _attempts_lock:
        pending = [a for a in _attempts.values() if not a.finished.is_set()]
    for attempt in pending:
        attempt.cancelled.set()
        if attempt.thread is not None:
            attempt.thread.join(timeout=5)


def wait_for_attempt(attempt_id: str, timeout: float) -> Dict[str, Any]:
    with _attempts_lock:
        attempt = _attempts.get(attempt_id)
    if attempt is None:
        raise OAuthError("unknown_attempt")
    attempt.finished.wait(timeout)
    return attempt.public()



@contextlib.contextmanager
def _refresh_guard():
    with _store_lock:
        if fcntl is None:
            yield
            return
        with open(os.path.join(credential_dir(), ".refresh.lock"), "a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _needs_refresh(record: Dict[str, Any]) -> bool:
    return int(record.get("expires_at") or 0) - _now() < REFRESH_MARGIN_SECONDS


def _clear_tokens(record: Dict[str, Any]) -> None:
    for key in ("access_token", "refresh_token", "id_token", "expires_at", "expires_in"):
        record.pop(key, None)


def _refresh(record: Dict[str, Any]) -> Dict[str, Any]:
    if not record.get("refresh_token"):
        raise OAuthError("reauth_required", "no refresh token")
    try:
        status, token = _post_form(TOKEN_URL, {
            "grant_type": "refresh_token",
            "client_id": record["client_id"],
            "refresh_token": record["refresh_token"],
            "resource": RESOURCE,
        })
    except requests.RequestException:
        raise OAuthError("refresh_failed", "network error during token refresh") from None
    if status == 200 and isinstance(token, dict) and token.get("access_token"):
        _apply_token_response(record, token)
        _save_record(record, make_active=False)
        return record
    code = _oauth_error_code(token, status)
    if code in TERMINAL_REFRESH_ERRORS:
        _clear_tokens(record)
        _save_record(record, make_active=False)
        raise OAuthError("reauth_required", code)
    raise OAuthError("refresh_failed", code)


def get_access_token() -> str:
    """Access token for the active account, refreshed when less than 5 minutes remain."""
    record = _load_active_record()
    if not record or not record.get("access_token"):
        raise OAuthError("not_signed_in")
    if PLAN_SCOPE not in record.get("scopes", []):
        raise OAuthError("plan_usage_disabled")
    if not _needs_refresh(record):
        return record["access_token"]
    with _refresh_guard():
        # Another thread/process may have refreshed while we waited for the lock.
        record = _load_active_record()
        if not record or not record.get("access_token"):
            raise OAuthError("not_signed_in")
        if _needs_refresh(record):
            record = _refresh(record)
        return record["access_token"]


def list_models() -> List[Dict[str, str]]:
    """Models the signed-in account may use: [{"slug", "display_name"}], server order kept."""
    token = get_access_token()
    try:
        response = requests.get(
            MODELS_URL, headers={"Authorization": f"Bearer {token}"}, timeout=HTTP_TIMEOUT
        )
    except requests.RequestException:
        raise OAuthError("models_unavailable", "network error") from None
    if response.status_code != 200:
        raise OAuthError("models_unavailable", f"http_{response.status_code}")
    try:
        models = response.json().get("models") or []
    except (ValueError, AttributeError):
        raise OAuthError("models_unavailable", "invalid response") from None
    return [
        {"slug": str(m["slug"]), "display_name": str(m.get("display_name") or m["slug"])}
        for m in models
        if isinstance(m, dict) and m.get("visibility") == "list" and m.get("slug")
    ]


def _cached_models(client_id: str) -> Tuple[List[Dict[str, str]], Optional[str]]:
    cached = _models_cache.get(client_id)
    if cached and _now() - cached[0] < MODELS_CACHE_SECONDS:
        return cached[1], None
    try:
        models = list_models()
    except OAuthError as error:
        logger.warning("ChatGPT model list unavailable: %s", error.code)
        return [], error.code
    _models_cache[client_id] = (_now(), models)
    return models, None


def get_status(attempt_id: Optional[str] = None) -> Dict[str, Any]:
    """Token-free status: {"signed_in", "email", "models", "expires_at", ...}."""
    record = _load_active_record() or {}
    signed_in = bool(record.get("access_token") and record.get("refresh_token"))
    plan_enabled = bool(signed_in and PLAN_SCOPE in record.get("scopes", []))
    models: List[Dict[str, str]] = []
    models_error: Optional[str] = None
    if plan_enabled:
        models, models_error = _cached_models(record["client_id"])
        record = _load_active_record() or record
    expires_at = record.get("expires_at") if signed_in else None

    with _attempts_lock:
        chosen = attempt_id or _current_attempt_id
        attempt = _attempts.get(chosen) if chosen else None
    return {
        "signed_in": signed_in,
        "email": record.get("email"),
        "models": models,
        "models_error": models_error,
        "expires_at": (
            datetime.fromtimestamp(int(expires_at), timezone.utc).isoformat() if expires_at else None
        ),
        "plan_usage_enabled": plan_enabled,
        "registered": bool(record.get("client_id")),
        "attempt": attempt.public() if attempt else None,
        "usage_url": "https://chatgpt.com/settings/usage",
    }


def _revoke(record: Dict[str, Any]) -> bool:
    try:
        endpoint = _openid_configuration().get("revocation_endpoint")
    except (requests.RequestException, ValueError):
        endpoint = None
    if not endpoint:
        return False
    for attempt in range(3):
        try:
            status, _ = _post_form(endpoint, {
                "token": record["refresh_token"],
                "token_type_hint": "refresh_token",
                "client_id": record["client_id"],
            })
        except requests.RequestException:
            status = 0
        if status == 200:
            return True
        if 0 < status < 500:
            return False
        time.sleep(0.5 * (2 ** attempt))
    return False


def sign_out() -> Dict[str, Any]:
    """Revoke the renewable session, then clear tokens; keep the client/account mapping."""
    cancel_pending_sign_in()
    with _refresh_guard():
        record = _load_active_record()
        if not record:
            return {"signed_out": True, "revoked": False}
        revoked = _revoke(record) if record.get("refresh_token") else False
        _clear_tokens(record)
        _save_record(record, make_active=True)
        _models_cache.pop(record["client_id"], None)
    return {"signed_out": True, "revoked": revoked}
