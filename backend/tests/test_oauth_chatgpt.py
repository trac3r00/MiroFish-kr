import base64
import hashlib
import json
import os
import stat
import threading
import time
from typing import Any
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import jwt
import jwt.algorithms
import pytest
import requests
from cryptography.hazmat.primitives.asymmetric import rsa

import app.llm.oauth_chatgpt as oauth

ISSUED = "oaiapp_test123"
PLAN_SCOPES = "chatgpt.tokens.use.direct email offline_access openid profile resource.invoke"


def _private_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


SIGNING_KEY = _private_key()
OTHER_KEY = _private_key()


def _jwk(key, kid):
    data = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    data.update({"kid": kid, "alg": "RS256", "use": "sig"})
    return data


class FakeAuthServer:
    def __init__(self):
        self.models_auth: Any = None
        self.token_requests = []
        self.revoke_requests = []
        self.token_handler: Any = None
        self.models = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                return

            def _json(self, status, body):
                raw = json.dumps(body).encode() if body is not None else b""
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                if self.path == "/.well-known/openid-configuration":
                    self._json(200, {"issuer": oauth.ISSUER, "jwks_uri": outer.url + "/jwks",
                                     "revocation_endpoint": outer.url + "/revoke"})
                elif self.path == "/jwks":
                    self._json(200, {"keys": [_jwk(SIGNING_KEY, "k1")]})
                elif self.path == "/v1/models":
                    outer.models_auth = self.headers.get("Authorization")
                    self._json(200, {"models": outer.models})
                else:
                    self._json(404, {})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
                if self.path == "/token":
                    outer.token_requests.append(form)
                    status, body = outer.token_handler(form)
                    self._json(status, body)
                elif self.path == "/revoke":
                    outer.revoke_requests.append(form)
                    self._json(200, None)
                else:
                    self._json(404, {})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def id_token(*, client_id=ISSUED, nonce, sub="user-1", email="u@example.com", key=SIGNING_KEY,
             iss=oauth.ISSUER, exp_delta=3600):
    now = int(time.time())
    claims = {"iss": iss, "aud": client_id, "sub": sub, "email": email, "nonce": nonce,
              "iat": now, "exp": now + exp_delta}
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "k1"})


@pytest.fixture
def fake(monkeypatch, tmp_path):
    monkeypatch.setenv("MIROFISH_CREDENTIAL_DIR", str(tmp_path / "cred"))
    monkeypatch.setattr(oauth, "PREFERRED_PORT", 0)
    server = FakeAuthServer()
    monkeypatch.setattr(oauth, "TOKEN_URL", server.url + "/token")
    monkeypatch.setattr(oauth, "OPENID_CONFIGURATION_URL", server.url + "/.well-known/openid-configuration")
    monkeypatch.setattr(oauth, "MODELS_URL", server.url + "/v1/models")
    oauth._attempts.clear()
    oauth._models_cache.clear()
    oauth._oidc_cache.clear()
    yield server
    oauth.cancel_pending_sign_in()
    oauth._attempts.clear()
    server.close()


def authorize_params(start):
    return {k: v[0] for k, v in parse_qs(urlsplit(start["authorize_url"]).query).items()}


def code_response(nonce_holder, *, scope=PLAN_SCOPES, client_id=ISSUED, sub="user-1", refresh="rt-1"):
    def handler(form):
        if form["grant_type"] != "authorization_code":
            return 400, {"error": "unsupported_grant_type"}
        return 200, {"access_token": "at-1", "refresh_token": refresh, "token_type": "Bearer",
                     "expires_in": 3600, "scope": scope,
                     "id_token": id_token(client_id=client_id, nonce=nonce_holder(), sub=sub)}
    return handler


def callback(params, **query):
    return requests.get(params["redirect_uri"], params=query, timeout=10)


def attempt_of(start):
    return oauth._attempts[start["attempt_id"]]


def sign_in_new(fake, **overrides):
    start = oauth.start_sign_in()
    params = authorize_params(start)
    fake.token_handler = code_response(lambda: params["nonce"], **overrides)
    response = callback(params, code="c1", state=params["state"], client_id=ISSUED, scope=PLAN_SCOPES)
    result = oauth.wait_for_attempt(start["attempt_id"], 10)
    return start, params, response, result


def account_file():
    return os.path.join(oauth.credential_dir(), f"account-{ISSUED}.json")


def test_first_registration_authorize_url_and_listener_ready(fake):
    start = oauth.start_sign_in()
    params = authorize_params(start)
    attempt = attempt_of(start)

    assert start["authorize_url"].startswith(oauth.AUTHORIZE_URL + "?")
    assert params["client_id"] == "dynamic_agent_client"
    assert params["agent_name_hint"] == "MiroFish"
    assert params["ext_agent_host_id"].startswith("urn:uuid:")
    assert params["ext_agent_host_id"] == oauth.get_host_id()
    assert "id_token_hint" not in params
    assert params["scope"] == "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
    assert params["resource"] == "https://api.openai.com/v1"
    assert params["response_type"] == "code"
    assert params["code_challenge_method"] == "S256"
    expected = base64.urlsafe_b64encode(hashlib.sha256(attempt.code_verifier.encode()).digest()).rstrip(b"=")
    assert params["code_challenge"] == expected.decode()
    redirect = urlsplit(params["redirect_uri"])
    assert (redirect.scheme, redirect.hostname, redirect.path) == ("http", "127.0.0.1", "/auth/callback")
    assert requests.get(f"http://127.0.0.1:{redirect.port}/other", timeout=5).status_code == 404

    second = authorize_params(oauth.start_sign_in())
    assert second["state"] != params["state"] and second["nonce"] != params["nonce"]
    assert second["ext_agent_host_id"] == params["ext_agent_host_id"]
    assert attempt_of(start).status == "cancelled"


def test_state_mismatch_rejected_then_valid_callback_succeeds(fake):
    start = oauth.start_sign_in()
    params = authorize_params(start)
    fake.token_handler = code_response(lambda: params["nonce"])

    bad = callback(params, code="c1", state="forged", client_id=ISSUED)
    assert bad.status_code == 400
    assert fake.token_requests == []
    assert attempt_of(start).status == "pending"
    assert not os.path.exists(account_file())

    good = callback(params, code="c1", state=params["state"], client_id=ISSUED)
    assert good.status_code == 200
    assert "Signed in - you can close this tab" in good.text
    assert oauth.wait_for_attempt(start["attempt_id"], 10)["status"] == "success"
    exchange = fake.token_requests[0]
    assert exchange == {
        "grant_type": "authorization_code", "client_id": ISSUED, "code": "c1",
        "code_verifier": attempt_of(start).code_verifier,
        "redirect_uri": params["redirect_uri"], "resource": "https://api.openai.com/v1",
    }


def test_access_denied_stops_without_exchange(fake):
    start = oauth.start_sign_in()
    params = authorize_params(start)
    response = callback(params, error="access_denied", state=params["state"])
    result = oauth.wait_for_attempt(start["attempt_id"], 10)
    assert response.status_code == 400
    assert result["status"] == "error" and result["error"] == "access_denied"
    assert fake.token_requests == []
    assert oauth.get_status()["signed_in"] is False


@pytest.mark.parametrize("client_id", [None, "dynamic_agent_client"])
def test_new_registration_without_issued_client_is_incomplete(fake, client_id):
    start = oauth.start_sign_in()
    params = authorize_params(start)
    query = {"code": "c1", "state": params["state"]}
    if client_id:
        query["client_id"] = client_id
    callback(params, **query)
    result = oauth.wait_for_attempt(start["attempt_id"], 10)
    assert result["error"] == "registration_incomplete"
    assert fake.token_requests == []
    assert [n for n in os.listdir(oauth.credential_dir()) if n.startswith("account-")] == []


def test_success_saves_0600_record_and_status_has_no_tokens(fake):
    _, _, response, result = sign_in_new(fake)
    assert response.status_code == 200 and result == {
        "attempt_id": result["attempt_id"], "status": "success", "error": None, "plan_usage_enabled": True}

    path = account_file()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    record = json.load(open(path))
    assert record["client_id"] == ISSUED
    assert record["subject"] == "user-1" and record["email"] == "u@example.com"
    assert record["ext_agent_host_id"] == oauth.get_host_id()
    assert record["scopes"] == sorted(PLAN_SCOPES.split())
    assert record["refresh_token"] == "rt-1"
    for name in os.listdir(oauth.credential_dir()):
        assert "dynamic_agent_client" not in open(os.path.join(oauth.credential_dir(), name)).read()

    fake.models = [{"slug": "gpt-a", "display_name": "GPT A", "visibility": "list"},
                   {"slug": "hidden", "display_name": "H", "visibility": "hide"}]
    status = oauth.get_status()
    assert status["signed_in"] and status["plan_usage_enabled"] and status["email"] == "u@example.com"
    assert status["models"] == [{"slug": "gpt-a", "display_name": "GPT A"}]
    assert fake.models_auth == "Bearer at-1"
    dumped = json.dumps(status)
    assert "at-1" not in dumped and "rt-1" not in dumped and record["id_token"] not in dumped


def test_atomic_write_keeps_old_file_on_failure(fake, monkeypatch):
    sign_in_new(fake)
    before = open(account_file()).read()

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(oauth.os, "replace", boom)
    record = json.loads(before)
    record["access_token"] = "changed"
    with pytest.raises(OSError):
        oauth._save_record(record, make_active=False)
    assert open(account_file()).read() == before
    assert [n for n in os.listdir(oauth.credential_dir()) if n.startswith(".tmp-")] == []


def test_invalid_id_tokens_rejected(fake):
    for bad_token in ("nonce", "signature", "audience"):
        start = oauth.start_sign_in()
        params = authorize_params(start)

        def handler(form, bad_token=bad_token):
            token = {
                "nonce": id_token(nonce="wrong"),
                "signature": id_token(nonce=params["nonce"], key=OTHER_KEY),
                "audience": id_token(nonce=params["nonce"], client_id="oaiapp_other"),
            }[bad_token]
            return 200, {"access_token": "at", "refresh_token": "rt", "expires_in": 3600,
                         "scope": PLAN_SCOPES, "id_token": token}

        fake.token_handler = handler
        callback(params, code="c", state=params["state"], client_id=ISSUED)
        assert oauth.wait_for_attempt(start["attempt_id"], 10)["error"] == "invalid_id_token", bad_token
    assert not os.path.exists(account_file())


def test_missing_plan_scope_keeps_sign_in_but_blocks_inference(fake):
    _, _, _, result = sign_in_new(fake, scope="openid profile email offline_access")
    assert result["status"] == "success" and result["plan_usage_enabled"] is False
    status = oauth.get_status()
    assert status["signed_in"] is True and status["plan_usage_enabled"] is False
    with pytest.raises(oauth.OAuthError) as error:
        oauth.get_access_token()
    assert error.value.code == "plan_usage_disabled"


def test_returning_sign_in_uses_issued_client_and_checks_identity(fake):
    _, first, _, _ = sign_in_new(fake)
    saved = json.load(open(account_file()))

    start = oauth.start_sign_in()
    params = authorize_params(start)
    assert params["client_id"] == ISSUED
    assert params["id_token_hint"] == saved["id_token"]
    assert params["login_hint"] == "u@example.com"
    assert "agent_name_hint" not in params
    assert params["ext_agent_host_id"] == first["ext_agent_host_id"]

    callback(params, code="c2", state=params["state"], client_id="oaiapp_someone_else")
    assert oauth.wait_for_attempt(start["attempt_id"], 10)["error"] == "client_mismatch"

    start = oauth.start_sign_in()
    params = authorize_params(start)
    fake.token_handler = code_response(lambda: params["nonce"], sub="different-user")
    callback(params, code="c3", state=params["state"])
    assert oauth.wait_for_attempt(start["attempt_id"], 10)["error"] == "account_mismatch"
    assert json.load(open(account_file()))["access_token"] == saved["access_token"]


def _expire_soon():
    record = json.load(open(account_file()))
    record["expires_at"] = int(time.time()) + 60
    oauth._save_record(record, make_active=False)


def test_refresh_rotates_tokens_and_is_serialized(fake, monkeypatch):
    sign_in_new(fake)
    _expire_soon()
    entered, release = threading.Event(), threading.Event()
    refresh_calls = []

    def refresh_handler(form):
        refresh_calls.append(form)
        entered.set()
        release.wait(10)
        return 200, {"access_token": "at-2", "refresh_token": "rt-2", "expires_in": 3600,
                     "scope": PLAN_SCOPES}

    fake.token_handler = refresh_handler
    second_saw_stale = threading.Event()
    original_needs_refresh = oauth._needs_refresh

    def watching_needs_refresh(record):
        result = original_needs_refresh(record)
        if threading.current_thread().name == "second" and result:
            second_saw_stale.set()
        return result

    monkeypatch.setattr(oauth, "_needs_refresh", watching_needs_refresh)
    results = {}

    def call(name):
        results[name] = oauth.get_access_token()

    first = threading.Thread(target=call, args=("first",), name="first")
    second = threading.Thread(target=call, args=("second",), name="second")
    first.start()
    assert entered.wait(10)
    second.start()
    assert second_saw_stale.wait(10)
    release.set()
    first.join(10)
    second.join(10)

    assert results == {"first": "at-2", "second": "at-2"}
    assert len(refresh_calls) == 1
    assert refresh_calls[0] == {"grant_type": "refresh_token", "client_id": ISSUED,
                                "refresh_token": "rt-1", "resource": "https://api.openai.com/v1"}
    record = json.load(open(account_file()))
    assert (record["access_token"], record["refresh_token"]) == ("at-2", "rt-2")
    assert record["expires_at"] > time.time() + 3000
    assert stat.S_IMODE(os.stat(account_file()).st_mode) == 0o600


def test_refresh_errors(fake):
    sign_in_new(fake)
    _expire_soon()
    fake.token_handler = lambda form: (503, {"error": "temporarily_unavailable"})
    with pytest.raises(oauth.OAuthError) as error:
        oauth.get_access_token()
    assert error.value.code == "refresh_failed"
    assert json.load(open(account_file()))["refresh_token"] == "rt-1"

    fake.token_handler = lambda form: (400, {"error": "invalid_grant"})
    with pytest.raises(oauth.OAuthError) as error:
        oauth.get_access_token()
    assert error.value.code == "reauth_required"
    record = json.load(open(account_file()))
    assert "refresh_token" not in record and "access_token" not in record
    assert record["client_id"] == ISSUED


def test_sign_out_revokes_and_keeps_registration(fake):
    sign_in_new(fake)
    assert oauth.sign_out() == {"signed_out": True, "revoked": True}
    assert fake.revoke_requests == [{"token": "rt-1", "token_type_hint": "refresh_token", "client_id": ISSUED}]
    record = json.load(open(account_file()))
    assert not any(k in record for k in ("access_token", "refresh_token", "id_token"))
    assert oauth.get_status()["signed_in"] is False

    params = authorize_params(oauth.start_sign_in())
    assert params["client_id"] == ISSUED
    assert "id_token_hint" not in params and "agent_name_hint" not in params
