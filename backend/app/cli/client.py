"""Thin stdlib HTTP client for the MiroFish API (the same endpoints the web UI uses)."""

from __future__ import annotations

import json
import mimetypes
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

DEFAULT_API = "http://127.0.0.1:5001"


class ApiError(RuntimeError):
    def __init__(self, message: str, status: Optional[int] = None, body: Optional[dict] = None):
        super().__init__(message)
        self.status = status
        self.body = body or {}


class Api:
    def __init__(self, base_url: Optional[str] = None, lang: str = "en", timeout: float = 120.0):
        self.base_url = (base_url or os.environ.get("MIROFISH_API") or DEFAULT_API).rstrip("/")
        self.lang = lang
        self.timeout = timeout

    def _headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        headers = {"Accept-Language": self.lang}
        headers.update(extra or {})
        return headers

    def _send(self, request: urllib.request.Request) -> Tuple[int, Dict[str, str], bytes]:
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers or {}), error.read()
        except urllib.error.URLError as error:
            raise ApiError(f"cannot reach {self.base_url}: {error.reason}") from error

    def call(self, method: str, path: str, body: Any = None, query: Optional[dict] = None) -> dict:
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = self._headers({"Content-Type": "application/json"} if data is not None else None)
        status, _headers, raw = self._send(urllib.request.Request(url, data=data, method=method, headers=headers))
        return self._envelope(status, raw)

    @staticmethod
    def _envelope(status: int, raw: bytes) -> dict:
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            raise ApiError(f"HTTP {status}: {raw[:200]!r}", status)
        # 202 "still in progress" answers are not failures for the caller.
        if status >= 400 or (payload.get("success") is False and status != 202):
            raise ApiError(str(payload.get("error") or payload.get("message") or f"HTTP {status}"), status, payload)
        payload["_status"] = status
        return payload

    def get(self, path: str, **query) -> dict:
        return self.call("GET", path, query=query or None)

    def post(self, path: str, body: Optional[dict] = None) -> dict:
        return self.call("POST", path, body or {})

    def upload(self, path: str, files: Iterable[Path], fields: Dict[str, str]) -> dict:
        boundary = uuid.uuid4().hex
        chunks = []
        for name, value in fields.items():
            chunks.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode("utf-8")
            )
        for file in files:
            mime = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
            chunks.append(
                (f"--{boundary}\r\nContent-Disposition: form-data; name=\"files\"; filename=\"{file.name}\"\r\n"
                 f"Content-Type: {mime}\r\n\r\n").encode("utf-8") + file.read_bytes() + b"\r\n"
            )
        chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
        request = urllib.request.Request(
            self.base_url + path,
            data=b"".join(chunks),
            method="POST",
            headers=self._headers({"Content-Type": f"multipart/form-data; boundary={boundary}"}),
        )
        status, _headers, raw = self._send(request)
        return self._envelope(status, raw)

    def download(self, path: str) -> Tuple[str, bytes]:
        status, headers, raw = self._send(urllib.request.Request(self.base_url + path, headers=self._headers()))
        if status >= 400:
            self._envelope(status, raw)
        return filename_from_disposition(headers.get("Content-Disposition", "")), raw


def filename_from_disposition(value: str) -> str:
    match = re.search(r"filename\*=UTF-8''([^;]+)", value, re.IGNORECASE)
    if match:
        return urllib.parse.unquote(match.group(1))
    match = re.search(r'filename="?([^";]+)"?', value)
    return match.group(1) if match else "report.md"
