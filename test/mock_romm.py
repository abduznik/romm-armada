# SPDX-License-Identifier: MIT
"""An in-process stand-in for the RomM endpoints the plugin uses.

It follows RomM's own code (backend/endpoints/saves.py, sync/__init__.py, handler/sync/comparison.py,
device/auth.py, utils/client_tokens.py) closely enough that the plugin's sync logic meets the same answers
it would get from a real server. The real server is exercised separately (test/real_romm.sh).
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
import time
import urllib.parse
import zlib
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PAIR_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
ALL_SCOPES = ["me.read", "me.write", "roms.read", "platforms.read", "assets.read", "assets.write",
              "devices.read", "devices.write", "roms.user.read", "roms.user.write"]


def utc(dt: datetime | None = None) -> datetime:
    return (dt or datetime.now(timezone.utc)).astimezone(timezone.utc)


def iso(dt: datetime) -> str:
    return utc(dt).isoformat().replace("+00:00", "Z")


def parse(s: str) -> datetime:
    return utc(datetime.fromisoformat(s.replace("Z", "+00:00")))


def compare_save_state(client_hash, client_updated_at, server_hash, server_updated_at, device_last_synced_at,
                       device_last_sync_hash=None, device_last_sync_server_hash=None):
    """handler/sync/comparison.py, without the removal bookkeeping."""
    if client_hash and server_hash and client_hash == server_hash:
        return "no_op", "Content is identical"
    if device_last_synced_at:
        client_unchanged = bool(client_hash and device_last_sync_hash and client_hash == device_last_sync_hash)
        server_unchanged = bool(server_hash and device_last_sync_server_hash and server_hash == device_last_sync_server_hash)
        client_changed = client_updated_at > device_last_synced_at and not client_unchanged
        server_changed = server_updated_at > device_last_synced_at and not server_unchanged
        if client_changed and server_changed:
            return "conflict", "Both sides changed since last sync"
        if client_changed:
            return "upload", "Client save is newer than last sync"
        if server_changed:
            return "download", "Server save is newer than last sync"
        return "no_op", "No changes since last sync"
    if client_updated_at > server_updated_at:
        return "upload", "Client save is newer (no sync history)"
    if server_updated_at > client_updated_at:
        return "download", "Server save is newer (no sync history)"
    if client_hash != server_hash:
        return "conflict", "Same timestamp but different content"
    return "no_op", "Saves appear identical"


class Fail(Exception):
    def __init__(self, status, detail=""):
        self.status, self.detail = status, detail


class MockRomm:
    def __init__(self, version="5.4.1"):
        self.version = version
        self.lock = threading.RLock()
        self.platforms: dict[int, dict] = {}
        self.roms: dict[int, dict] = {}
        self.file_data: dict[int, bytes] = {}
        self.next_id = 100
        self.tokens: dict[str, list[str]] = {}
        self.pair_codes: dict[str, dict] = {}
        self.pair_attempts: list[float] = []
        self.flows: dict[str, dict] = {}
        self.devices: dict[str, dict] = {}
        self.syncs: dict[tuple[str, int], dict] = {}
        self.saves: list[dict] = []
        self.sessions: dict[int, dict] = {}
        self.play_sessions: list[dict] = []
        self.install_requests: list[dict] = []
        self.assets: dict[str, bytes] = {}            # static files RomM serves without a token (covers)
        self.requests: list[tuple[str, str]] = []     # (method, path) log for assertions
        self.fail_downloads = 0                       # next N file requests answer 503
        self.drop_after: int | None = None            # cut the next file response after this many bytes
        self.ignore_range = False
        self.userpass = ("tester", "secret")
        self.clock_offset = timedelta(0)
        self.httpd: ThreadingHTTPServer | None = None
        self.url = ""

    # -- test helpers ----------------------------------------------------------------------------------

    def now(self) -> datetime:
        return utc() + self.clock_offset

    def make_token(self, scopes=None, device_id=None) -> str:
        tok = "rmm_" + secrets.token_hex(12)
        self.tokens[tok] = list(scopes if scopes is not None else ALL_SCOPES)
        if device_id:
            self.tokens[tok + "#device"] = [device_id]
        return tok

    def issue_pair_code(self, scopes=None, ttl=60) -> str:
        code = "".join(secrets.choice(PAIR_ALPHABET) for _ in range(8))
        tok = self.make_token(scopes)
        self.pair_codes[code] = {"token": tok, "expires": time.time() + ttl, "scopes": list(scopes or ALL_SCOPES)}
        return code

    def approve_flow(self, user_code: str, scopes=None):
        for dc, f in self.flows.items():
            if f["user_code"] == user_code.replace("-", "").upper():
                dev_id = secrets.token_hex(8)
                self.devices[dev_id] = {"id": dev_id, "name": f["name"], "hostname": "", "platform": f.get("platform"),
                                        "client": f.get("client"), "sync_enabled": True, "created_at": iso(self.now())}
                f["status"] = "approved"
                f["token"] = self.make_token(scopes, dev_id)
                f["device_id"] = dev_id
                return dev_id
        raise KeyError(user_code)

    def deny_flow(self, user_code: str):
        for f in self.flows.values():
            if f["user_code"] == user_code.replace("-", "").upper():
                f["status"] = "denied"

    def add_platform(self, slug="ps2", name="PlayStation 2", fs_slug=None) -> int:
        pid = len(self.platforms) + 1
        self.platforms[pid] = {"id": pid, "slug": slug, "fs_slug": fs_slug or slug, "name": name, "display_name": name,
                               "custom_name": None, "rom_count": 0, "family_name": "PlayStation"}
        return pid

    def add_rom(self, platform_id: int, name: str, files: list[tuple[str, bytes]] | None = None, folder=False,
                title_id=None) -> int:
        """files: (relative path inside the ROM, data). Single file: its name is the ROM's fs_name."""
        rid = self._id()
        p = self.platforms[platform_id]
        files = files or [(f"{name}.iso", f"ISO:{name}".encode() * 100)]
        multi = folder or len(files) > 1
        fs_name = name if multi else files[0][0]
        base = f"roms/{p['fs_slug']}/{fs_name}" if multi else f"roms/{p['fs_slug']}/{fs_name}"
        fl = []
        for rel, data in files:
            fid = self._id()
            self.file_data[fid] = data
            fname = rel.split("/")[-1]
            fpath = f"roms/{p['fs_slug']}/{fs_name}" + ("/" + "/".join(rel.split("/")[:-1]) if "/" in rel else "") if multi else f"roms/{p['fs_slug']}"
            fl.append({"id": fid, "rom_id": rid, "file_name": fname, "file_path": fpath, "full_path": fpath + "/" + fname,
                       "file_size_bytes": len(data), "is_top_level": True, "category": "game",
                       "crc_hash": format(zlib.crc32(data) & 0xFFFFFFFF, "08x"), "md5_hash": hashlib.md5(data).hexdigest(),
                       "sha1_hash": hashlib.sha1(data).hexdigest()})
        self.roms[rid] = {
            "id": rid, "name": name, "fs_name": fs_name, "fs_name_no_ext": fs_name.rsplit(".", 1)[0], "fs_path": f"roms/{p['fs_slug']}",
            "fs_size_bytes": sum(len(d) for _, d in files), "platform_id": platform_id, "platform_slug": p["slug"],
            "platform_fs_slug": p["fs_slug"], "platform_display_name": p["name"], "platform_custom_name": None,
            "has_multiple_files": len(files) > 1, "has_nested_single_file": folder and len(files) == 1,
            "has_simple_single_file": not multi, "full_path": base, "title_id": title_id, "files": fl,
            "path_cover_small": None, "path_cover_large": None, "url_cover": None, "regions": [], "languages": [],
        }
        p["rom_count"] += 1
        return rid

    def _id(self) -> int:
        self.next_id += 1
        return self.next_id

    def saves_of(self, rom_id, slot=None):
        return [s for s in self.saves if s["rom_id"] == rom_id and (slot is None or s["slot"] == slot)]

    def seed_save(self, rom_id, data: bytes, slot="autosave", emulator="armsx2_refresh", age=timedelta(0), file_name="game.ps2"):
        """A save already on the server, put there by 'another device'."""
        s = {"id": self._id(), "rom_id": rom_id, "file_name": file_name, "emulator": emulator, "slot": slot,
             "content_hash": hashlib.md5(data).hexdigest(), "data": data, "updated_at": self.now() - age,
             "origin_device_id": "other-device", "file_size_bytes": len(data)}
        self.saves.append(s)
        return s

    # -- server --------------------------------------------------------------------------------------------

    def start(self):
        mock = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                mock.handle(self, "GET")

            def do_POST(self):
                mock.handle(self, "POST")

            def do_PUT(self):
                mock.handle(self, "PUT")

            def do_DELETE(self):
                mock.handle(self, "DELETE")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self.url

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()

    # -- dispatch ------------------------------------------------------------------------------------------

    def handle(self, h: BaseHTTPRequestHandler, method: str):
        u = urllib.parse.urlsplit(h.path)
        q = urllib.parse.parse_qs(u.query, keep_blank_values=True)
        length = int(h.headers.get("Content-Length") or 0)
        body = h.rfile.read(length) if length else b""
        with self.lock:
            self.requests.append((method, u.path))
        try:
            status, headers, payload = self.route(h, method, urllib.parse.unquote(u.path), q, body)
        except Fail as f:
            status, headers, payload = f.status, {}, json.dumps({"detail": f.detail}).encode()
            headers["Content-Type"] = "application/json"
        except BrokenPipeError:
            return
        if payload is None:  # a streaming handler already answered
            return
        h.send_response(status)
        headers.setdefault("Content-Type", "application/json")
        headers["Content-Length"] = str(len(payload))
        for k, v in headers.items():
            h.send_header(k, v)
        h.end_headers()
        if method != "HEAD":
            h.wfile.write(payload)

    def auth(self, h, scope=None) -> list[str]:
        a = h.headers.get("Authorization", "")
        if not a.startswith("Bearer "):
            raise Fail(401, "Not authenticated")
        tok = a[7:]
        if tok not in self.tokens:
            raise Fail(401, "Invalid token")
        scopes = self.tokens[tok]
        if scope and scope not in scopes:
            raise Fail(403, "Insufficient scope")
        return scopes

    def bound_device(self, h) -> str | None:
        tok = h.headers.get("Authorization", "")[7:]
        d = self.tokens.get(tok + "#device")
        return d[0] if d else None

    def j(self, obj, status=200):
        return status, {}, json.dumps(obj, default=str).encode()

    def route(self, h, method, path, q, body):  # noqa: C901
        g = lambda k, d=None: (q.get(k) or [d])[0]  # noqa: E731
        js = lambda: json.loads(body.decode() or "{}")  # noqa: E731
        # --- open endpoints
        if path.startswith("/assets/") and path in self.assets:
            return 200, {"Content-Type": "image/webp"}, self.assets[path]
        if path == "/api/heartbeat":
            return self.j({"SYSTEM": {"VERSION": self.version}, "FRONTEND": {"DISABLE_USERPASS_LOGIN": False},
                           "DEVICE_INSTALL": {"ENABLED": True}})
        if path == "/api/client-tokens/exchange" and method == "POST":
            now = time.time()
            self.pair_attempts = [t for t in self.pair_attempts if now - t < 60]
            if len(self.pair_attempts) >= 5:
                raise Fail(429, "Too many attempts")
            self.pair_attempts.append(now)
            code = js().get("code", "").replace("-", "").upper()
            info = self.pair_codes.pop(code, None)
            if not info or info["expires"] < now:
                raise Fail(404, "Invalid or expired pairing code")
            return self.j({"raw_token": info["token"], "name": "paired", "scopes": info["scopes"], "id": 1})
        if path == "/api/auth/device/init" and method == "POST":
            d = js()
            dc, uc = secrets.token_hex(16), "".join(secrets.choice(PAIR_ALPHABET) for _ in range(8))
            self.flows[dc] = {"user_code": uc, "status": "pending", "name": d["name"], "platform": d.get("platform"),
                              "client": d.get("client"), "identifier": d["client_device_identifier"],
                              "scopes": d["requested_scopes"], "last_poll": 0.0}
            return self.j({"device_code": dc, "user_code": uc, "verification_path": "/pair/device",
                           "verification_path_complete": f"/pair/device?user_code={uc}", "expires_in": 600, "interval": 5}, 201)
        if path == "/api/auth/device/token" and method == "POST":
            f = self.flows.get(js().get("device_code"))
            if not f:
                raise Fail(400, "expired_token")
            if f["status"] == "pending":
                raise Fail(400, "authorization_pending")
            if f["status"] == "denied":
                raise Fail(400, "access_denied")
            return self.j({"access_token": f["token"], "device_id": f["device_id"], "scopes": ALL_SCOPES, "expires_at": None})
        if path == "/api/token" and method == "POST":
            form = urllib.parse.parse_qs(body.decode())
            if form.get("grant_type", [""])[0] != "password" or \
                    (form.get("username", [""])[0], form.get("password", [""])[0]) != self.userpass:
                raise Fail(401, "Invalid username or password")
            tok = self.make_token(form.get("scope", [""])[0].split())
            return self.j({"access_token": tok, "refresh_token": "r", "token_type": "bearer", "expires": 1800})
        # --- authenticated
        if path == "/api/client-tokens" and method == "POST":
            self.auth(h, "me.write")
            d = js()
            return self.j({"raw_token": self.make_token(d.get("scopes")), "name": d.get("name"), "scopes": d.get("scopes")}, 201)
        if path == "/api/users/me":
            self.auth(h, "me.read")
            return self.j({"id": 1, "username": "tester", "role": "admin"})
        if path == "/api/platforms":
            self.auth(h, "platforms.read")
            return self.j(list(self.platforms.values()))
        if path == "/api/devices" and method == "POST":
            self.auth(h, "devices.write")
            d = js()
            for dev in self.devices.values():
                if dev.get("hostname") == d.get("hostname") and dev.get("platform") == d.get("platform") and dev.get("hostname"):
                    return self.j({"device_id": dev["id"], "name": dev["name"], "created_at": dev["created_at"]}, 200)
            did = secrets.token_hex(8)
            self.devices[did] = {"id": did, "name": d.get("name"), "hostname": d.get("hostname"), "platform": d.get("platform"),
                                 "client": d.get("client"), "sync_enabled": True, "capabilities": d.get("capabilities"),
                                 "created_at": iso(self.now())}
            return self.j({"device_id": did, "name": d.get("name"), "created_at": self.devices[did]["created_at"]}, 201)
        m = re.fullmatch(r"/api/devices/([^/]+)", path)
        if m and method == "PUT":
            self.auth(h, "devices.write")
            dev = self.devices.get(m.group(1)) or self._404("Device")
            dev.update(js())
            return self.j(dev)
        m = re.fullmatch(r"/api/devices/([^/]+)/installs/claim", path)
        if m and method == "POST":
            self.auth(h, "devices.write")
            out = [r for r in self.install_requests if r["device_id"] == m.group(1) and r["status"] in ("pending", "taken")]
            for r in out:
                r["status"] = "taken"
            return self.j(out)
        m = re.fullmatch(r"/api/devices/([^/]+)/installs/([^/]+)", path)
        if m and method == "PUT":
            self.auth(h, "devices.write")
            for r in self.install_requests:
                if r["id"] == m.group(2):
                    r["status"] = js()["status"]
                    return self.j(r)
            raise Fail(404, "Install request not found")
        if path == "/api/roms" and method == "GET":
            self.auth(h, "roms.read")
            return self.list_roms(q)
        m = re.fullmatch(r"/api/roms/(\d+)", path)
        if m and method == "GET":
            self.auth(h, "roms.read")
            return self.j(self.roms.get(int(m.group(1))) or self._404("Rom"))
        m = re.fullmatch(r"/api/roms/(\d+)/files/content/(.+)", path)
        if m and method == "GET":
            self.auth(h, "roms.read")
            return self.send_file(h, int(m.group(1)))
        # --- saves
        if path == "/api/saves" and method == "GET":
            self.auth(h, "assets.read")
            return self.j(self.save_list(g("rom_id"), g("device_id"), g("slot")))
        if path == "/api/saves" and method == "POST":
            self.auth(h, "assets.write")
            return self.add_save(h, q, body)
        m = re.fullmatch(r"/api/saves/(\d+)/content", path)
        if m:
            self.auth(h, "assets.read")
            s = next((x for x in self.saves if x["id"] == int(m.group(1))), None) or self._404("Save")
            dev = g("device_id")
            if dev and g("optimistic", "true") == "true":
                self.record_sync(dev, s)
            return 200, {"Content-Type": "application/octet-stream"}, s["data"]
        m = re.fullmatch(r"/api/saves/(\d+)/downloaded", path)
        if m and method == "POST":
            self.auth(h, "devices.write")
            s = next((x for x in self.saves if x["id"] == int(m.group(1))), None) or self._404("Save")
            d = js()
            key = (d["device_id"], s["id"])
            served = self.syncs.get(key)
            ch, sh, at = d.get("content_hash"), s["content_hash"], s["updated_at"]
            if served and served.get("server_hash") and not served.get("hash"):
                at, sh = served["at"], served["server_hash"]
            elif ch != sh:
                ch = sh = None
            self.syncs[key] = {"at": at, "hash": ch, "server_hash": sh}
            return self.j(self.save_schema(s, d["device_id"]))
        # --- sync
        if path == "/api/sync/negotiate" and method == "POST":
            self.auth(h, "assets.read")
            return self.negotiate(js())
        m = re.fullmatch(r"/api/sync/sessions/(\d+)/complete", path)
        if m and method == "POST":
            self.auth(h, "devices.write")
            ses = self.sessions.get(int(m.group(1))) or self._404("Sync session")
            d = js()
            ses.update(status="completed", completed=d.get("operations_completed"), failed=d.get("operations_failed"))
            return self.j({"session": ses})
        if path == "/api/play-sessions" and method == "POST":
            self.auth(h, "roms.user.write")
            d = js()
            self.play_sessions += d["sessions"]
            return self.j({"results": [], "created_count": len(d["sessions"]), "skipped_count": 0}, 201)
        raise Fail(404, f"No route for {method} {path}")

    def _404(self, what):
        raise Fail(404, f"{what} not found")

    # -- roms ------------------------------------------------------------------------------------------------

    def list_roms(self, q):
        pids = [int(x) for x in q.get("platform_ids", [])]
        term = (q.get("search_term") or [""])[0].lower()
        limit, offset = int((q.get("limit") or [50])[0]), int((q.get("offset") or [0])[0])
        with_files = (q.get("with_files") or ["false"])[0] == "true"
        items = [r for r in self.roms.values() if (not pids or r["platform_id"] in pids) and term in (r["name"] or "").lower()]
        items.sort(key=lambda r: (r["name"] or "").lower(), reverse=(q.get("order_dir") or ["asc"])[0] == "desc")
        page = items[offset:offset + limit]
        if not with_files:
            page = [{k: v for k, v in r.items() if k != "files"} for r in page]
        return self.j({"items": page, "total": len(items), "limit": limit, "offset": offset})

    def send_file(self, h, file_id):
        data = self.file_data.get(file_id)
        if data is None:
            raise Fail(404, "File not found")
        if self.fail_downloads > 0:
            self.fail_downloads -= 1
            raise Fail(503, "Try later")
        start = 0
        rng = h.headers.get("Range")
        status = 200
        if rng and not self.ignore_range:
            m = re.fullmatch(r"bytes=(\d+)-(\d*)", rng)
            if m:
                start = int(m.group(1))
                if start >= len(data):
                    raise Fail(416, "Range not satisfiable")
                status = 206
        chunk = data[start:]
        h.send_response(status)
        h.send_header("Content-Type", "application/octet-stream")
        h.send_header("Content-Length", str(len(chunk)))
        h.send_header("Accept-Ranges", "bytes")
        if status == 206:
            h.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
        h.end_headers()
        if self.drop_after is not None:
            cut, self.drop_after = self.drop_after, None
            h.wfile.write(chunk[:cut])
            h.wfile.flush()
            h.close_connection = True
            try:
                h.connection.shutdown(2)
            except OSError:
                pass
            return 0, {}, None
        h.wfile.write(chunk)
        return 0, {}, None

    # -- saves --------------------------------------------------------------------------------------------------

    def save_schema(self, s, device_id=None):
        out = {k: v for k, v in s.items() if k != "data"}
        out["updated_at"] = iso(s["updated_at"])
        out["download_path"] = f"/api/saves/{s['id']}/content"
        out["device_syncs"] = []
        if device_id:
            for (dev, sid), sy in self.syncs.items():
                if sid == s["id"]:
                    out["device_syncs"].append({"device_id": dev, "device_name": self.devices.get(dev, {}).get("name"),
                                                "last_synced_at": iso(sy["at"]), "is_untracked": False,
                                                "is_current": sy["at"] >= s["updated_at"]})
        return out

    def save_list(self, rom_id, device_id, slot):
        items = [s for s in self.saves if (rom_id is None or s["rom_id"] == int(rom_id)) and (slot is None or s["slot"] == slot)]
        items.sort(key=lambda s: s["updated_at"], reverse=True)
        return [self.save_schema(s, device_id) for s in items]

    def record_sync(self, device_id, s, client_hash=None):
        self.syncs[(device_id, s["id"])] = {"at": s["updated_at"], "hash": client_hash, "server_hash": s["content_hash"]}

    def add_save(self, h, q, body):
        g = lambda k, d=None: (q.get(k) or [d])[0]  # noqa: E731
        rom_id = int(g("rom_id"))
        if rom_id not in self.roms:
            raise Fail(404, f"ROM with id {rom_id} not found")
        emulator, slot, device_id = g("emulator"), g("slot"), g("device_id")
        overwrite = g("overwrite", "false") == "true"
        if device_id and device_id not in self.devices:
            raise Fail(404, f"Device with ID {device_id} not found")
        ctype = h.headers.get("Content-Type", "")
        m = re.search(r"boundary=(.+)", ctype)
        if not m:
            raise Fail(422, "multipart body expected")
        parts = body.split(b"--" + m.group(1).encode())
        data, filename = None, None
        for part in parts:
            if b'name="saveFile"' in part:
                head, _, rest = part.partition(b"\r\n\r\n")
                fn = re.search(rb'filename="([^"]*)"', head)
                filename = fn.group(1).decode() if fn else "save"
                data = rest[:-2] if rest.endswith(b"\r\n") else rest
        if data is None:
            raise Fail(422, "saveFile missing")
        digest = hashlib.md5(data).hexdigest()
        slot_saves = sorted([s for s in self.saves if s["rom_id"] == rom_id and s["slot"] == slot],
                            key=lambda s: s["updated_at"], reverse=True) if slot else []
        if device_id and slot and not overwrite and slot_saves:
            latest = slot_saves[0]
            sy = self.syncs.get((device_id, latest["id"]))
            if not sy or sy["at"] < latest["updated_at"]:
                raise Fail(409, "Slot has a newer save since your last sync")
        if slot:
            existing = next((s for s in slot_saves if s["content_hash"] == digest), None)
            if existing and not overwrite:
                existing["updated_at"] = self.now()
                if device_id:
                    self.record_sync(device_id, existing, g("content_hash"))
                self._prune(rom_id, slot, q, emulator)
                return self.j(self.save_schema(existing, device_id))
        stored = filename
        if slot:
            stem, dot, ext = filename.rpartition(".")
            tag = self.now().strftime("[%Y-%m-%d_%H-%M-%S-%f]")
            stored = f"{stem} {tag}.{ext}" if dot else f"{filename} {tag}"
        s = {"id": self._id(), "rom_id": rom_id, "file_name": stored, "emulator": emulator, "slot": slot,
             "content_hash": digest, "data": data, "updated_at": self.now(), "origin_device_id": device_id,
             "file_size_bytes": len(data)}
        self.saves.append(s)
        if device_id:
            self.record_sync(device_id, s, g("content_hash"))
        if g("session_id"):
            ses = self.sessions.get(int(g("session_id")))
            if ses:
                ses["ops"] = ses.get("ops", 0) + 1
        self._prune(rom_id, slot, q, emulator)
        return self.j(self.save_schema(s, device_id))

    def _prune(self, rom_id, slot, q, emulator):
        if not slot or (q.get("autocleanup") or ["false"])[0] != "true":
            return
        limit = int((q.get("autocleanup_limit") or [10])[0])
        mine = sorted([s for s in self.saves if s["rom_id"] == rom_id and s["slot"] == slot and s["emulator"] == emulator],
                      key=lambda s: s["updated_at"], reverse=True)
        for old in mine[limit:]:
            self.saves.remove(old)

    # -- negotiate --------------------------------------------------------------------------------------------------

    def negotiate(self, d):
        dev_id = d.get("device_id")
        device = self.devices.get(dev_id)
        if not device:
            raise Fail(404, f"Device with ID {dev_id} not found")
        sid = len(self.sessions) + 1
        self.sessions[sid] = {"id": sid, "device_id": dev_id, "status": "in_progress"}
        scope = d.get("rom_ids")
        rom_scope = (scope + [s["rom_id"] for s in d["saves"]]) if scope is not None else None
        server = [s for s in self.saves if s["slot"] is not None and (rom_scope is None or s["rom_id"] in rom_scope)]
        if d.get("emulators") is not None:
            server = [s for s in server if s["emulator"] in d["emulators"]]
        smap: dict[tuple, dict] = {}
        for s in server:
            k = (s["rom_id"], s["slot"])
            if k not in smap or s["updated_at"] > smap[k]["updated_at"]:
                smap[k] = s
        ops, matched = [], set()
        for c in d["saves"]:
            k = (c["rom_id"], c.get("slot"))
            s = smap.get(k)
            cts = parse(c["updated_at"])
            if s is None:
                ops.append({"action": "upload", "rom_id": c["rom_id"], "save_id": None, "file_name": c["file_name"],
                            "slot": c.get("slot"), "emulator": c.get("emulator"), "reason": "Save exists on client but not on server"})
                continue
            matched.add(s["id"])
            sy = self.syncs.get((dev_id, s["id"]))
            action, reason = compare_save_state(c.get("content_hash"), cts, s["content_hash"], s["updated_at"],
                                                sy["at"] if sy else None, sy["hash"] if sy else None,
                                                sy["server_hash"] if sy else None)
            if action == "no_op" and c.get("content_hash") == s["content_hash"]:
                self.syncs[(dev_id, s["id"])] = {"at": s["updated_at"], "hash": s["content_hash"], "server_hash": s["content_hash"]}
            ops.append({"action": action, "rom_id": s["rom_id"], "save_id": s["id"], "file_name": s["file_name"], "slot": s["slot"],
                        "emulator": s["emulator"], "reason": reason, "server_updated_at": iso(s["updated_at"]),
                        "server_content_hash": s["content_hash"]})
        for s in smap.values():
            if s["id"] in matched:
                continue
            sy = self.syncs.get((dev_id, s["id"]))
            if sy and s["updated_at"] <= sy["at"] and not d.get("restore_unlisted"):
                continue
            ops.append({"action": "download", "rom_id": s["rom_id"], "save_id": s["id"], "file_name": s["file_name"],
                        "slot": s["slot"], "emulator": s["emulator"],
                        "reason": "Save exists on server but not on client" if not sy else "Server save updated since last sync",
                        "server_updated_at": iso(s["updated_at"]), "server_content_hash": s["content_hash"]})
        counts = {a: sum(1 for o in ops if o["action"] == a) for a in ("upload", "download", "conflict", "no_op", "delete")}
        return self.j({"session_id": sid, "operations": ops, "total_upload": counts["upload"], "total_download": counts["download"],
                       "total_conflict": counts["conflict"], "total_no_op": counts["no_op"], "total_delete": counts["delete"]})
