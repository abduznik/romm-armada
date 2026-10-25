# SPDX-License-Identifier: MIT
"""A small HTTP client on the standard library.

It runs inside Decky's own Python, where installing packages is not an option, so this is urllib with
the three things the plugin needs spelled out: certificate handling that works in a bundled
interpreter, redirects that never carry the token to another host, and streaming for downloads.
"""
from __future__ import annotations

import http.client
import json
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Iterable

from . import log

MAX_JSON_BYTES = 64 * 1024 * 1024


class NetworkError(Exception):
    """The server could not be reached or the connection broke. `kind` says roughly why."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


class HttpError(Exception):
    """The server answered with an error status."""

    def __init__(self, status: int, detail: str = "", body: bytes = b"", url: str = "") -> None:
        super().__init__(f"HTTP {status}: {detail}" if detail else f"HTTP {status}")
        self.status = status
        self.detail = detail
        self.body = body
        self.url = url


class Response:
    def __init__(self, status: int, headers: dict[str, str], body: bytes, url: str) -> None:
        self.status = status
        self.headers = headers
        self.body = body
        self.url = url

    def json(self) -> Any:
        if not self.body:
            return None
        return json.loads(self.body.decode("utf-8"))


def make_ssl_context(verify: bool = True) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    try:
        loaded = ctx.cert_store_stats().get("x509_ca", 0)
    except Exception:
        loaded = 0
    if not loaded:  # a bundled interpreter may not find the system store; Decky ships certifi
        try:
            import certifi  # type: ignore

            ctx.load_verify_locations(cafile=certifi.where())
        except Exception:
            pass
    return ctx


class _Redirects(urllib.request.HTTPRedirectHandler):
    """Follows redirects, but never takes the Authorization header to another origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802 (urllib's name)
        if req.get_method() not in ("GET", "HEAD") and code in (301, 302, 303):
            raise urllib.error.HTTPError(req.full_url, code,
                                         f"redirected to {newurl}; use the address the server redirects to",
                                         headers, fp)
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            old = urllib.parse.urlsplit(req.full_url)
            to = urllib.parse.urlsplit(newurl)
            if (old.scheme, old.netloc) != (to.scheme, to.netloc):
                new.headers.pop("Authorization", None)
                new.unredirected_hdrs.pop("Authorization", None)
        return new


def _detail(body: bytes) -> str:
    if not body:
        return ""
    try:
        doc = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return body[:200].decode("utf-8", "replace").strip()
    d = doc.get("detail") if isinstance(doc, dict) else None
    if isinstance(d, str):
        return d
    if isinstance(d, dict):
        return str(d.get("message") or d.get("error") or d)
    if isinstance(d, list) and d:
        first = d[0]
        return str(first.get("msg", first)) if isinstance(first, dict) else str(first)
    return ""


def _classify(exc: BaseException) -> NetworkError:
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, ssl.SSLCertVerificationError) or isinstance(exc, ssl.SSLCertVerificationError):
        return NetworkError("tls", "The server's certificate could not be verified")
    if isinstance(reason, ssl.SSLError):
        return NetworkError("tls", f"TLS error: {reason}")
    if isinstance(reason, (socket.timeout, TimeoutError)) or isinstance(exc, (socket.timeout, TimeoutError)):
        return NetworkError("timeout", "The server did not answer in time")
    if isinstance(reason, socket.gaierror):
        return NetworkError("dns", "The server's name could not be found")
    if isinstance(reason, ConnectionRefusedError):
        return NetworkError("refused", "The server refused the connection")
    if isinstance(reason, OSError) and getattr(reason, "errno", None) in (101, 113):  # net / host unreachable
        return NetworkError("unreachable", "The server cannot be reached from here")
    if isinstance(exc, (http.client.HTTPException, ConnectionError)):
        return NetworkError("connection", f"The connection broke: {exc}")
    return NetworkError("other", str(reason or exc))


def encode_query(params: dict[str, Any] | None) -> str:
    if not params:
        return ""
    pairs: list[tuple[str, str]] = []
    for key, value in params.items():
        if value is None:
            continue
        values = value if isinstance(value, (list, tuple)) else [value]
        for v in values:
            if isinstance(v, bool):
                v = "true" if v else "false"
            pairs.append((key, str(v)))
    return urllib.parse.urlencode(pairs)


def multipart(fields: dict[str, str] | None, files: Iterable[tuple[str, str, bytes, str]]) -> tuple[bytes, str]:
    """(body, content-type) for a multipart/form-data upload. files: (field, filename, data, mime)."""
    boundary = "----rommarmada" + uuid.uuid4().hex
    parts: list[bytes] = []
    for name, value in (fields or {}).items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    for field, filename, data, mime in files:
        safe = filename.replace('"', "_").replace("\r", " ").replace("\n", " ")
        head = (f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{safe}"\r\n'
                f"Content-Type: {mime}\r\n\r\n").encode("utf-8")
        parts.append(head + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


class HttpClient:
    def __init__(self, base_url: str, token: str = "", verify_tls: bool = True, timeout: float = 30.0,
                 user_agent: str = "RomM-Armada") -> None:
        self.base = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.user_agent = user_agent
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),  # a handheld on a LAN or tailnet: never a system proxy
            urllib.request.HTTPSHandler(context=make_ssl_context(verify_tls)),
            _Redirects(),
        )

    def url(self, path: str, params: dict[str, Any] | None = None) -> str:
        url = self.base + (path if path.startswith("/") else "/" + path)
        q = encode_query(params)
        return url + ("?" + q if q else "")

    def _build(self, method: str, url: str, headers: dict[str, str] | None, data: bytes | None) -> urllib.request.Request:
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("User-Agent", self.user_agent)
        req.add_header("Accept", "application/json, */*;q=0.5")
        if self.token:
            req.add_header("Authorization", "Bearer " + self.token)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        return req

    def open(self, method: str, path: str, *, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None,
             data: bytes | None = None, timeout: float | None = None):
        """The raw response for streaming; the caller closes it. Raises HttpError / NetworkError."""
        url = self.url(path, params)
        req = self._build(method, url, headers, data)
        try:
            return self._opener.open(req, timeout=timeout or self.timeout)
        except urllib.error.HTTPError as e:
            body = b""
            try:
                body = e.read(65536)
            except Exception:
                pass
            finally:
                e.close()
            raise HttpError(e.code, _detail(body) or (e.reason if isinstance(e.reason, str) else ""), body, url) from None
        except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
            raise _classify(e) from None

    def request(self, method: str, path: str, *, params: dict[str, Any] | None = None, json_body: Any = None,
                form: dict[str, str] | None = None, data: bytes | None = None, headers: dict[str, str] | None = None,
                timeout: float | None = None, retries: int | None = None) -> Response:
        hdrs = dict(headers or {})
        body = data
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        elif form is not None:
            body = urllib.parse.urlencode(form).encode("utf-8")
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        attempts = (2 if method in ("GET", "HEAD") else 0) if retries is None else retries
        delay = 0.5
        while True:
            try:
                resp = self.open(method, path, params=params, headers=hdrs, data=body, timeout=timeout)
                try:
                    raw = resp.read(MAX_JSON_BYTES + 1)
                    if len(raw) > MAX_JSON_BYTES:
                        raise NetworkError("other", "The response is too large")
                    status = getattr(resp, "status", 200)
                    out_headers = {k.lower(): v for k, v in resp.headers.items()}
                    return Response(status, out_headers, raw, self.url(path, params))
                finally:
                    resp.close()
            except (NetworkError, HttpError) as e:
                retryable = isinstance(e, NetworkError) and e.kind in ("timeout", "connection", "unreachable") \
                    or isinstance(e, HttpError) and e.status in (502, 503, 504)
                if attempts <= 0 or not retryable:
                    raise
                attempts -= 1
                log.debug("retrying %s %s after %s", method, path, e)
                time.sleep(delay)
                delay *= 3
            except (OSError, http.client.HTTPException) as e:  # a read that broke mid-body
                if attempts <= 0:
                    raise _classify(e) from None
                attempts -= 1
                time.sleep(delay)
                delay *= 3

    def get_json(self, path: str, **kw) -> Any:
        return self.request("GET", path, **kw).json()

    def post_json(self, path: str, body: Any = None, **kw) -> Any:
        return self.request("POST", path, json_body=body if body is not None else {}, **kw).json()
