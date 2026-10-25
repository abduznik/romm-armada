# SPDX-License-Identifier: MIT
"""Connecting this device to a RomM server.

Four ways in, from most to least handheld-friendly:

  pair_with_code    type the 8-character code RomM's web UI shows under API tokens > Pair. It lives 60
                    seconds and works once, so the exchange happens the moment it is typed.
  start_device_flow show a code on this device and approve it in RomM's web UI; poll_device_flow finishes.
  sign_in_token     paste a client token made in the web UI.
  sign_in_password  a username and password, used once to mint a client token; neither is kept.

Whichever way, the result is one client token in the settings and a device id registered on the server.
"""
from __future__ import annotations

import os
import socket
import threading
import time
import uuid
from typing import Callable

from . import log, paths
from .config import Settings, normalize_server_url, SettingsError
from .http import HttpClient
from .romm import (PAIRING_SCOPES, PAIR_CODE_TTL_SECONDS, RommClient, RommError, check_pair_code, describe,
                   parse_version)
from .util import read_json, write_json

PLUGIN_VERSION = "0.2.1"


def device_identifier() -> str:
    """A stable id for this install, so pairing again finds the same device on the server."""
    path = paths.state_dir() / "device.json"
    doc = read_json(path, None)
    if isinstance(doc, dict) and isinstance(doc.get("identifier"), str) and doc["identifier"]:
        return doc["identifier"]
    ident = "romm-armada-" + uuid.uuid4().hex
    write_json(path, {"identifier": ident})
    return ident


def hostname() -> str:
    try:
        return socket.gethostname() or "handheld"
    except OSError:
        return "handheld"


def romm_hostname() -> str:
    """The hostname this install registers under. RomM treats two devices with the same hostname and platform as one
    device (so would two handhelds that are both called "armada"), which would stop the second from ever being offered
    the first one's saves. The install id makes it unique and, being stable, lets a re-pair find the same device."""
    return f"{hostname()}-{device_identifier().rsplit('-', 1)[-1][:6]}"


class ConnectError(Exception):
    """Something the user can act on; `code` lets the UI pick the wording and the field."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class Connector:
    def __init__(self, settings: Settings, client_factory: Callable[[str, str, bool, float], RommClient] | None = None) -> None:
        self.settings = settings
        self._factory = client_factory or self._default_factory
        self._flow: dict | None = None
        self._lock = threading.Lock()

    @staticmethod
    def _default_factory(url: str, token: str, verify: bool, timeout: float) -> RommClient:
        return RommClient(HttpClient(url, token=token, verify_tls=verify, timeout=timeout,
                                     user_agent=f"RomM-Armada/{PLUGIN_VERSION}"))

    def _client(self, url: str, token: str = "", verify: bool | None = None) -> RommClient:
        verify = self.settings.get("server.verify_tls") if verify is None else verify
        return self._factory(url, token, verify, float(self.settings.get("advanced.request_timeout")))

    # -- the server ------------------------------------------------------------------------

    def _url(self, url: str) -> str:
        try:
            clean = normalize_server_url(url or self.settings.get("server.url"))
        except SettingsError as e:
            raise ConnectError("bad_url", f"The server address {e}") from None
        if not clean:
            raise ConnectError("bad_url", "Enter the address of your RomM server first")
        return clean

    def probe(self, url: str = "", verify_tls: bool | None = None) -> dict:
        """Reaches the server without signing in: its version, and which ways of signing in it offers."""
        clean = self._url(url)
        try:
            hb = self._client(clean, "", verify_tls).heartbeat()
        except RommError as e:
            raise ConnectError("unreachable", describe(e)) from None
        version = RommClient.version_of(hb)
        major = parse_version(version)[0]
        oidc_only = bool((hb.get("FRONTEND") or {}).get("DISABLE_USERPASS_LOGIN"))
        return {
            "url": clean, "version": version or "unknown",
            "supports_pairing": major >= 5 or version in ("development", ""),
            "password_login": not oidc_only,
            "device_install": bool((hb.get("DEVICE_INSTALL") or {}).get("ENABLED")),
        }

    # -- finishing a sign-in ---------------------------------------------------------------

    def _finish(self, url: str, token: str, method: str, verify: bool, device_id: str = "",
                scopes: list[str] | None = None) -> dict:
        client = self._client(url, token, verify)
        try:
            me = client.me()
        except RommError as e:
            if e.kind == "auth":
                raise ConnectError("rejected", "RomM did not accept that token") from None
            raise ConnectError("unreachable", describe(e)) from None
        name = self.settings.get("auth.device_name")
        if not device_id:
            try:
                dev = client.register_device(name, romm_hostname(), PLUGIN_VERSION,
                                             capabilities={"remote_install": bool(self.settings.get("library.remote_installs"))})
            except RommError as e:
                if e.kind == "forbidden":
                    raise ConnectError("scopes", "The token cannot register this device. It needs the devices.write "
                                                 "permission; make a new token with all of: " + ", ".join(PAIRING_SCOPES)) from None
                raise ConnectError("device", describe(e)) from None
            device_id = str((dev or {}).get("device_id") or (dev or {}).get("id") or "")
            if not device_id:
                raise ConnectError("device", "RomM did not give this device an id")
        missing = self._missing_scopes(client)
        self.settings.update({
            "server": {"url": url, "verify_tls": verify},
            "auth": {"token": token, "device_id": device_id, "username": str((me or {}).get("username") or ""),
                     "method": method},
        })
        log.info("signed in to %s as %s by %s, device %s", url, (me or {}).get("username"), method, device_id)
        return {"username": (me or {}).get("username") or "", "device_id": device_id, "method": method,
                "missing_scopes": missing, "scopes": scopes or []}

    @staticmethod
    def _missing_scopes(client: RommClient) -> list[str]:
        """Reads that the plugin needs, tried once; a 403 names a permission the token lacks."""
        probes = [("platforms.read", lambda: client.platforms()),
                  ("roms.read", lambda: client.roms(limit=1)),
                  ("assets.read", lambda: client.saves(0)),
                  ("firmware.read", lambda: client.firmware(0))]
        missing: list[str] = []
        for scope, call in probes:
            try:
                call()
            except RommError as e:
                if e.kind == "forbidden":
                    missing.append(scope)
            except Exception:
                pass
        return missing

    # -- 1. a typed pairing code -----------------------------------------------------------

    def pair_with_code(self, url: str, code: str, verify_tls: bool | None = None) -> dict:
        clean = self._url(url)
        verify = self.settings.get("server.verify_tls") if verify_tls is None else verify_tls
        try:
            normalized = check_pair_code(code)
        except ValueError as e:
            raise ConnectError("bad_code", str(e)) from None
        client = self._client(clean, "", verify)
        try:
            got = client.exchange_pair_code(normalized)
        except RommError as e:
            if e.kind == "not_found":
                raise ConnectError("code_expired", f"That code is not valid any more. Codes last {PAIR_CODE_TTL_SECONDS} "
                                                   "seconds and work once; make a new one in RomM and type it right away") from None
            if e.status == 429:
                raise ConnectError("rate_limited", "Too many tries. Wait a minute, then make a new code") from None
            if e.kind in ("unsupported",) or (e.kind == "client" and e.status in (405, 422)):
                raise ConnectError("unsupported", "This RomM does not take pairing codes. Use a token instead, "
                                                  "or update RomM to version 5") from None
            raise ConnectError("unreachable", describe(e)) from None
        token = got.get("token") or ""
        if not token:
            raise ConnectError("code_expired", "RomM accepted the code but sent no token. Make a new code")
        return self._finish(clean, token, "pairing", verify, scopes=got.get("scopes"))

    # -- 2. approve a code shown here -------------------------------------------------------

    def start_device_flow(self, url: str, verify_tls: bool | None = None) -> dict:
        clean = self._url(url)
        verify = self.settings.get("server.verify_tls") if verify_tls is None else verify_tls
        try:
            doc = self._client(clean, "", verify).device_auth_init(
                device_identifier(), self.settings.get("auth.device_name"), "linux", PLUGIN_VERSION)
        except RommError as e:
            if e.kind in ("not_found", "unsupported"):
                raise ConnectError("unsupported", "This RomM cannot show a code to approve. Type a pairing code "
                                                  "from the web UI instead") from None
            raise ConnectError("unreachable", describe(e)) from None
        now = time.time()
        flow = {
            "url": clean, "verify": verify, "device_code": doc["device_code"], "user_code": doc["user_code"],
            "verify_path": doc.get("verification_path_complete") or doc.get("verification_path") or "/pair/device",
            "expires_at": now + int(doc.get("expires_in") or 600), "interval": max(2, int(doc.get("interval") or 5)),
            "next_poll": now + 2,
        }
        with self._lock:
            self._flow = flow
        return self.flow_view()

    def flow_view(self) -> dict:
        with self._lock:
            f = self._flow
        if not f:
            return {"active": False}
        code = f["user_code"]
        return {"active": True, "user_code": f"{code[:4]}-{code[4:]}" if len(code) == 8 else code,
                "verify_url": f["url"] + f["verify_path"], "seconds_left": max(0, int(f["expires_at"] - time.time()))}

    def cancel_device_flow(self) -> None:
        with self._lock:
            self._flow = None

    def poll_device_flow(self) -> dict:
        with self._lock:
            f = self._flow
        if not f:
            return {"status": "none"}
        now = time.time()
        if now >= f["expires_at"]:
            self.cancel_device_flow()
            return {"status": "expired"}
        if now < f["next_poll"]:
            return {"status": "pending", **self.flow_view()}
        client = self._client(f["url"], "", f["verify"])
        try:
            got = client.device_auth_poll(f["device_code"])
        except RommError as e:
            return {"status": "error", "message": describe(e), **self.flow_view()}
        status = got["status"]
        with self._lock:
            if self._flow is f:
                f["next_poll"] = time.time() + f["interval"] + (5 if status == "slow_down" else 0)
        if status in ("pending", "slow_down"):
            return {"status": "pending", **self.flow_view()}
        if status in ("denied", "expired"):
            self.cancel_device_flow()
            return {"status": status}
        # approved: the token is bound to a device RomM created at approval
        self.cancel_device_flow()
        done = self._finish(f["url"], got["token"], "pairing", f["verify"], device_id=got.get("device_id", ""),
                            scopes=got.get("scopes"))
        return {"status": "approved", **done}

    # -- 3. a pasted token / 4. a password ---------------------------------------------------

    def sign_in_token(self, url: str, token: str, verify_tls: bool | None = None) -> dict:
        clean = self._url(url)
        verify = self.settings.get("server.verify_tls") if verify_tls is None else verify_tls
        token = (token or "").strip()
        if not token:
            raise ConnectError("bad_token", "Paste the token first")
        return self._finish(clean, token, "token", verify)

    def sign_in_password(self, url: str, username: str, password: str, verify_tls: bool | None = None) -> dict:
        clean = self._url(url)
        verify = self.settings.get("server.verify_tls") if verify_tls is None else verify_tls
        if not username or not password:
            raise ConnectError("bad_credentials", "Enter the username and the password")
        client = self._client(clean, "", verify)
        try:
            access = client.password_login(username, password)
        except RommError as e:
            if e.kind in ("auth",) or e.status in (400, 401):
                raise ConnectError("bad_credentials", "RomM did not accept that username and password") from None
            raise ConnectError("unreachable", describe(e)) from None
        if not access:
            raise ConnectError("bad_credentials", "RomM sent no token")
        try:
            minted = self._client(clean, access, verify).create_client_token(
                f"{self.settings.get('auth.device_name')}", expires_in=None)
        except RommError as e:
            raise ConnectError("unsupported", "This RomM cannot make a device token from a password. Pair with a "
                                              "code or paste a token from the web UI instead: " + describe(e)) from None
        if not minted:
            raise ConnectError("unsupported", "RomM made no token")
        return self._finish(clean, minted, "password", verify)

    # -- out ----------------------------------------------------------------------------------

    def sign_out(self) -> None:
        self.cancel_device_flow()
        self.settings.clear_auth()
