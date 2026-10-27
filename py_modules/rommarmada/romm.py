# SPDX-License-Identifier: MIT
"""The parts of RomM's API the plugin uses, one method each.

The shapes follow RomM's own source (endpoints/saves.py, endpoints/sync, endpoints/device,
endpoints/roms) as of 5.4. Responses stay plain dicts: the server adds fields over time and the
plugin only reads the ones it names.
"""
from __future__ import annotations

import json
import re
from typing import Any

from . import log
from .http import HttpClient, HttpError, NetworkError, Response, multipart

CLIENT_NAME = "romm-armada"

# What the plugin asks a pairing approval for: browse and download, sync saves, tell the server about
# this device and about play time. Nothing that edits the library.
PAIRING_SCOPES = [
    "me.read", "roms.read", "platforms.read", "assets.read", "assets.write",
    "devices.read", "devices.write", "roms.user.read", "roms.user.write", "firmware.read",
]


class RommError(Exception):
    """A RomM call that failed. kind: auth, forbidden, not_found, conflict, unsupported, client, server, network."""

    def __init__(self, kind: str, message: str, status: int = 0, detail: str = "", net_kind: str = "") -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.status = status
        self.detail = detail
        self.net_kind = net_kind


def wrap_error(e: Exception) -> RommError:
    if isinstance(e, NetworkError):
        return RommError("network", e.message, net_kind=e.kind)
    assert isinstance(e, HttpError)
    s = e.status
    detail = e.detail
    if s == 401:
        return RommError("auth", "The server did not accept the sign-in", s, detail)
    if s == 403:
        return RommError("forbidden", detail or "Not allowed", s, detail)
    if s == 404:
        return RommError("not_found", detail or "Not found", s, detail)
    if s == 409:
        return RommError("conflict", detail or "Conflict", s, detail)
    if s in (405, 501):
        return RommError("unsupported", "The server does not support this", s, detail)
    if s >= 500:
        return RommError("server", detail or f"Server error {s}", s, detail)
    return RommError("client", detail or f"Request refused ({s})", s, detail)


PAIR_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no I, L, O, 0 or 1, as RomM generates them
PAIR_CODE_LENGTH = 8
PAIR_CODE_TTL_SECONDS = 60  # a code from "Pair" in RomM's web UI lives this long, and works once


def normalize_pair_code(text: str) -> str:
    """What the server compares: no dashes or spaces, upper case."""
    return re.sub(r"[\s\-_]+", "", text or "").upper()


def check_pair_code(text: str) -> str:
    """The normalized code, or a ValueError that says what is wrong with what was typed."""
    code = normalize_pair_code(text)
    if len(code) != PAIR_CODE_LENGTH:
        raise ValueError(f"A pairing code has {PAIR_CODE_LENGTH} characters, {len(code)} typed")
    bad = sorted({c for c in code if c not in PAIR_ALPHABET})
    if bad:
        raise ValueError("A pairing code never has " + ", ".join(bad) + " (it has no I, L, O, 0 or 1)")
    return code


def parse_version(text: str) -> tuple[int, int, int]:
    m = re.match(r"v?(\d+)\.(\d+)(?:\.(\d+))?", text or "")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)) if m else (0, 0, 0)


class RommClient:
    def __init__(self, http: HttpClient) -> None:
        self.http = http

    # -- plumbing --------------------------------------------------------------------------

    def _json(self, method: str, path: str, **kw) -> Any:
        try:
            return self.http.request(method, path, **kw).json()
        except (HttpError, NetworkError) as e:
            raise wrap_error(e) from None
        except ValueError:
            raise RommError("server", "The server sent something that is not JSON") from None

    # -- the server ------------------------------------------------------------------------

    def heartbeat(self) -> dict:
        return self._json("GET", "/api/heartbeat", retries=1)

    @staticmethod
    def version_of(heartbeat: dict) -> str:
        return str((heartbeat.get("SYSTEM") or {}).get("VERSION") or "")

    def me(self) -> dict:
        return self._json("GET", "/api/users/me")

    # -- signing in ------------------------------------------------------------------------

    def device_auth_init(self, identifier: str, name: str, platform: str, version: str,
                         scopes: list[str] | None = None) -> dict:
        return self._json("POST", "/api/auth/device/init", json_body={
            "client_device_identifier": identifier, "name": name, "client": CLIENT_NAME,
            "platform": platform, "client_version": version,
            "requested_scopes": scopes or PAIRING_SCOPES,
        })

    def device_auth_poll(self, device_code: str) -> dict:
        """{'status': 'pending'|'slow_down'|'denied'|'expired'|'approved', ...token fields when approved}."""
        try:
            r = self.http.request("POST", "/api/auth/device/token", json_body={"device_code": device_code}, retries=0)
            doc = r.json() or {}
            return {"status": "approved", "token": doc.get("access_token", ""), "device_id": doc.get("device_id", ""),
                    "scopes": doc.get("scopes", []), "expires_at": doc.get("expires_at")}
        except HttpError as e:
            if e.status == 400:
                code = (e.detail or "").strip()
                table = {"authorization_pending": "pending", "slow_down": "slow_down",
                         "access_denied": "denied", "expired_token": "expired"}
                if code in table:
                    return {"status": table[code]}
            raise wrap_error(e) from None
        except NetworkError as e:
            raise wrap_error(e) from None

    def exchange_pair_code(self, code: str) -> dict:
        """Trades a pairing code (RomM's web UI, API tokens, Pair) for the token behind it."""
        doc = self._json("POST", "/api/client-tokens/exchange", json_body={"code": check_pair_code(code)}, retries=0)
        return {"token": (doc or {}).get("raw_token", ""), "name": (doc or {}).get("name", ""),
                "scopes": (doc or {}).get("scopes") or []}

    def password_login(self, username: str, password: str, scopes: list[str] | None = None) -> str:
        """An access token from the OAuth password grant. Used once, to mint a client token."""
        doc = self._json("POST", "/api/token", retries=0, form={
            "grant_type": "password", "username": username, "password": password,
            "scope": " ".join(scopes or PAIRING_SCOPES + ["me.write"]),
        })
        return (doc or {}).get("access_token", "")

    def create_client_token(self, name: str, scopes: list[str] | None = None, expires_in: str | None = None) -> str:
        body: dict[str, Any] = {"name": name, "scopes": scopes or PAIRING_SCOPES}
        if expires_in:
            body["expires_in"] = expires_in
        doc = self._json("POST", "/api/client-tokens", json_body=body, retries=0)
        return (doc or {}).get("raw_token", "")

    # -- devices ---------------------------------------------------------------------------

    def register_device(self, name: str, hostname: str, version: str, platform: str = "linux",
                        capabilities: dict[str, bool] | None = None) -> dict:
        body: dict[str, Any] = {"name": name, "platform": platform, "client": CLIENT_NAME,
                                "client_version": version, "hostname": hostname, "allow_existing": True}
        if capabilities is not None:
            body["capabilities"] = capabilities
        return self._json("POST", "/api/devices", json_body=body, retries=0)

    def update_device(self, device_id: str, **fields) -> dict:
        return self._json("PUT", f"/api/devices/{device_id}", json_body=fields, retries=0)

    # -- library ---------------------------------------------------------------------------

    def platforms(self) -> list[dict]:
        return self._json("GET", "/api/platforms") or []

    def roms(self, platform_id: int | None = None, search: str = "", limit: int = 50, offset: int = 0,
             order_by: str = "name", order_dir: str = "asc", with_files: bool = False) -> dict:
        params = {
            "platform_ids": [platform_id] if platform_id is not None else None,
            "search_term": search or None, "limit": limit, "offset": offset,
            "order_by": order_by, "order_dir": order_dir, "with_files": with_files,
            # sidecars the plugin does not use and the server would otherwise compute
            "with_char_index": False, "with_filter_values": False, "with_rom_id_index": False,
            "group_by_meta_id": False,
        }
        return self._json("GET", "/api/roms", params=params)

    def rom(self, rom_id: int) -> dict:
        return self._json("GET", f"/api/roms/{rom_id}")

    def file_url_path(self, file_id: int, file_name: str) -> str:
        from urllib.parse import quote
        return f"/api/roms/{file_id}/files/content/{quote(file_name)}"

    def cover_url(self, rom: dict) -> str:
        path = rom.get("path_cover_small") or rom.get("path_cover_large") or ""
        if path.startswith("http"):
            return path
        if path:
            return self.http.base + "/assets/romm/resources/" + path.lstrip("/") if not path.startswith("/") else self.http.base + path
        return rom.get("url_cover") or ""

    # -- firmware (BIOS files) -----------------------------------------------------------------

    def firmware(self, platform_id: int) -> list[dict]:
        return self._json("GET", "/api/firmware", params={"platform_id": platform_id}) or []

    @staticmethod
    def firmware_path(fw: dict) -> str:
        from urllib.parse import quote
        return f"/api/firmware/{fw['id']}/content/{quote(fw['file_name'])}"

    # -- saves -----------------------------------------------------------------------------

    def saves(self, rom_id: int, device_id: str | None = None, slot: str | None = None) -> list[dict]:
        return self._json("GET", "/api/saves", params={"rom_id": rom_id, "device_id": device_id, "slot": slot}) or []

    def upload_save(self, rom_id: int, data: bytes, filename: str, *, emulator: str, slot: str, device_id: str,
                    content_hash: str = "", session_id: int | None = None, overwrite: bool = False,
                    autocleanup: bool = False, autocleanup_limit: int = 10) -> dict:
        body, ctype = multipart(None, [("saveFile", filename, data, "application/octet-stream")])
        params = {"rom_id": rom_id, "emulator": emulator, "slot": slot, "device_id": device_id,
                  "content_hash": content_hash or None, "session_id": session_id, "overwrite": overwrite,
                  "autocleanup": autocleanup, "autocleanup_limit": autocleanup_limit if autocleanup else None}
        try:
            r = self.http.request("POST", "/api/saves", params=params, data=body, headers={"Content-Type": ctype},
                                  timeout=max(60.0, self.http.timeout), retries=0)
            return r.json() or {}
        except (HttpError, NetworkError) as e:
            raise wrap_error(e) from None

    def download_save(self, save_id: int, device_id: str, session_id: int | None = None,
                      optimistic: bool = True) -> bytes:
        try:
            r = self.http.request("GET", f"/api/saves/{save_id}/content", timeout=max(60.0, self.http.timeout),
                                  params={"device_id": device_id, "session_id": session_id,
                                          "optimistic": optimistic})
            return r.body
        except (HttpError, NetworkError) as e:
            raise wrap_error(e) from None

    def confirm_download(self, save_id: int, device_id: str, content_hash: str) -> dict:
        return self._json("POST", f"/api/saves/{save_id}/downloaded", retries=0,
                          json_body={"device_id": device_id, "content_hash": content_hash})

    # -- save states (RomM keeps them apart from saves: one per file name, no slots, no devices) ---------

    def states(self, rom_id: int) -> list[dict]:
        return self._json("GET", "/api/states", params={"rom_id": rom_id}) or []

    def upload_state(self, rom_id: int, data: bytes, filename: str, emulator: str) -> dict:
        body, ctype = multipart(None, [("stateFile", filename, data, "application/octet-stream")])
        try:
            r = self.http.request("POST", "/api/states", params={"rom_id": rom_id, "emulator": emulator}, data=body,
                                  headers={"Content-Type": ctype}, timeout=max(120.0, self.http.timeout), retries=0)
            return r.json() or {}
        except (HttpError, NetworkError) as e:
            raise wrap_error(e) from None

    def download_state(self, state_id: int) -> bytes:
        try:
            return self.http.request("GET", f"/api/states/{state_id}/content", timeout=max(120.0, self.http.timeout)).body
        except (HttpError, NetworkError) as e:
            raise wrap_error(e) from None

    # -- the sync protocol -----------------------------------------------------------------

    def negotiate(self, device_id: str, saves: list[dict], rom_ids: list[int] | None = None,
                  emulators: list[str] | None = None, restore_unlisted: bool = False) -> dict:
        body: dict[str, Any] = {"device_id": device_id, "saves": saves, "restore_unlisted": restore_unlisted}
        if rom_ids is not None:
            body["rom_ids"] = rom_ids
        if emulators:
            body["emulators"] = emulators
        return self._json("POST", "/api/sync/negotiate", json_body=body, retries=0)

    def complete_session(self, session_id: int, completed: int, failed: int) -> dict:
        return self._json("POST", f"/api/sync/sessions/{session_id}/complete", retries=0,
                          json_body={"operations_completed": completed, "operations_failed": failed})

    def play_sessions(self, device_id: str, sessions: list[dict]) -> dict:
        return self._json("POST", "/api/play-sessions", json_body={"device_id": device_id, "sessions": sessions},
                          retries=0)

    # -- install requests (the web UI's "send to device") ---------------------------------------

    def claim_installs(self, device_id: str) -> list[dict]:
        return self._json("POST", f"/api/devices/{device_id}/installs/claim", retries=0) or []

    def report_install(self, device_id: str, request_id: str, status: str, reason: str | None = None) -> dict:
        body: dict[str, Any] = {"status": status}
        if reason:
            body["reason"] = reason
        return self._json("PUT", f"/api/devices/{device_id}/installs/{request_id}", json_body=body, retries=0)


def describe(e: Exception) -> str:
    """A sentence for the UI."""
    if isinstance(e, RommError):
        if e.kind == "network":
            return {"tls": "The server's certificate is not trusted. Check the address, or turn off certificate checks.",
                    "dns": "The server's name could not be found. Check the address.",
                    "refused": "The server refused the connection. Check the address and port.",
                    "timeout": "The server did not answer in time.",
                    "unreachable": "The server cannot be reached from this network."}.get(e.net_kind, e.message)
        if e.kind == "auth":
            return "The server did not accept this device's sign-in. Pair the device again."
        return e.message
    return str(e)
