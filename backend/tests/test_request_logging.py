import json
import logging
import random
from io import StringIO

from app import create_app
from app.utils.logger import format_request_body


def make_app(monkeypatch):
    monkeypatch.setattr(
        "app.services.simulation_runner.SimulationRunner.register_cleanup",
        lambda: None,
    )
    return create_app()


def test_body_logging_disabled_does_not_parse_json(monkeypatch, caplog):
    monkeypatch.delenv("LOG_REQUEST_BODIES", raising=False)
    app = make_app(monkeypatch)
    calls = 0
    original = app.request_class.get_json

    def counted(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        return original(self, *args, **kwargs)

    monkeypatch.setattr(app.request_class, "get_json", counted)
    caplog.set_level(logging.DEBUG, logger="mirofish.request")
    app.test_client().post("/health", json={"private": "must-not-appear"})
    assert calls == 0
    assert "must-not-appear" not in caplog.text


def test_enabled_body_logging_redacts_and_truncates(monkeypatch, caplog):
    monkeypatch.setenv("LOG_REQUEST_BODIES", "1")
    monkeypatch.setenv("LOG_BODY_MAX_CHARS", "80")
    app = make_app(monkeypatch)
    request_logger = logging.getLogger("mirofish.request")
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.setLevel(logging.DEBUG)
    request_logger.addHandler(handler)
    request_logger.setLevel(logging.DEBUG)
    app.test_client().post(
        "/health",
        json={"api_token": "sensitive-value", "message": "x" * 150},
    )
    request_logger.removeHandler(handler)
    record = stream.getvalue()
    assert "sensitive-value" not in record
    assert "***" in record
    assert "[truncated]" in record


def test_property_nested_json_redaction_never_logs_sensitive_values():
    rng = random.Random(10)
    attempted = 100
    executed = 0

    def make_body(depth=0):
        result = {}
        for index in range(rng.randint(1, 4)):
            secret = f"secret-value-{executed}-{depth}-{index}"
            key = rng.choice(["api_key", "token", "secret_note", "password", "authorization"])
            result[key] = {"nested": [secret, {"value": secret}]}
            result[f"ordinary_{index}"] = [rng.randint(0, 100), {"text": "public"}]
        return result

    for _ in range(attempted):
        body = make_body()
        secrets = [
            item["nested"][0]
            for key, item in body.items()
            if key in {"api_key", "token", "secret_note", "password", "authorization"}
        ]
        record = format_request_body(body)
        assert all(secret not in record for secret in secrets)
        parsed = json.loads(record)
        assert all(value == "***" for key, value in parsed.items() if key in body and key in {
            "api_key", "token", "secret_note", "password", "authorization"
        })
        executed += 1

    assert executed == attempted
