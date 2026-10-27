# SPDX-License-Identifier: MIT
"""The plugin's brain: one object main.py talks to. Everything here is blocking and returns plain
dicts; main.py runs each call in a thread and turns the dicts into the answers the UI reads."""
from __future__ import annotations

import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import bios, emulators, libretro, library as lib, log, paths, saves as savesmod
from .config import Settings
from .connect import PLUGIN_VERSION, ConnectError, Connector, romm_hostname
from .downloads import ACTIVE, DownloadManager
from .http import HttpClient
from .library import Layout, Manifest
from .romm import RommClient, RommError, describe
from .sigil import Savebridge, SavebridgeError, SigilRefusal
from .util import free_bytes, human_bytes, iso, now_utc, read_json, removable_volumes, volume_info, write_json

LIST_CACHE_TTL = 60.0


class Service:
    def __init__(self, emit: Callable[[str, dict], None] | None = None, bridge: Savebridge | None = None,
                 settings: Settings | None = None, proc_root: str = "/proc") -> None:
        self.proc_root = proc_root
        self._emit = emit or (lambda e, p: None)
        self.settings = settings or Settings()
        if settings is None:
            self.settings.load()
        self.manifest = Manifest()
        self.bridge = bridge or Savebridge()
        self.connector = Connector(self.settings)
        self._client: tuple[tuple, RommClient] | None = None
        self._lists: dict[tuple, tuple[float, Any]] = {}
        self._bios_lock = threading.Lock()
        self.downloads = DownloadManager(self.settings, self.manifest, self.client, self.emit, self._on_installed)
        self.saves = savesmod.SaveSync(self.settings, self.manifest, self.client, self.bridge, self.emit,
                                       reregister=self._reregister, proc_root=proc_root)
        from .watcher import Watcher
        self.watcher = Watcher(self)
        self.started = False

    # -- plumbing ----------------------------------------------------------------------------------------

    def emit(self, event: str, payload: dict) -> None:
        try:
            self._emit(event, payload)
        except Exception:  # noqa: BLE001 - a UI that cannot be told must not break the work
            log.exception("could not emit %s", event)

    def start(self) -> None:
        if self.started:
            return
        self.started = True
        self.downloads.start()
        self.watcher.start()
        log.info("RomM Armada %s started", PLUGIN_VERSION)
        threading.Thread(target=self.setup_cards, args=("start",), name="romm-cards", daemon=True).start()

    def stop(self) -> None:
        self.watcher.stop()
        self.downloads.stop()
        self.started = False

    def client(self) -> RommClient:
        s = self.settings
        if not s.signed_in:
            raise RommError("auth", "Connect to RomM first")
        key = (s.get("server.url"), s.get("auth.token"), s.get("server.verify_tls"), s.get("advanced.request_timeout"))
        if self._client and self._client[0] == key:
            return self._client[1]
        c = RommClient(HttpClient(key[0], token=key[1], verify_tls=key[2], timeout=float(key[3]),
                                  user_agent=f"RomM-Armada/{PLUGIN_VERSION}"))
        self._client = (key, c)
        return c

    def _reregister(self) -> str:
        s = self.settings
        dev = self.client().register_device(s.get("auth.device_name"), romm_hostname(), PLUGIN_VERSION,
                                            capabilities={"remote_install": bool(s.get("library.remote_installs"))})
        did = str(dev.get("device_id") or dev.get("id") or "")
        if did:
            s.update({"auth": {"device_id": did}})
        return did

    def layout(self) -> Layout:
        s = self.settings
        return Layout(s.get("library.root"), s.get("library.folder_style"), s.get("library.platform_dirs"))

    # -- overview -----------------------------------------------------------------------------------------

    def overview(self) -> dict:
        s = self.settings
        root = s.get("library.root")
        vol = volume_info(root)
        jobs = self.downloads.status()
        installed = self.manifest.all()
        resolved = emulators.resolve_ps2(s.get("emulators.ps2.emulator"), s.get("emulators.ps2.data_dir"),
                                         s.get("emulators.ps2.executable"))
        cfg = emulators.card_config(resolved.data_dir)
        conflicts = [c for c in (self._conflict_of(g["rom_id"]) for g in installed) if c]
        return {
            "version": PLUGIN_VERSION,
            "signed_in": s.signed_in,
            "server": {"url": s.get("server.url"), "username": s.get("auth.username"), "method": s.get("auth.method"),
                       "verify_tls": s.get("server.verify_tls")},
            "device": {"id": s.get("auth.device_id"), "name": s.get("auth.device_name")},
            "library": {"root": root, "free": vol["free"], "total": vol["total"], "installed": len(installed),
                        "installed_size": sum(int(g.get("size") or 0) for g in installed)},
            "downloads": {"active": sum(1 for j in jobs if j["state"] in ACTIVE), "total": len(jobs),
                          "failed": sum(1 for j in jobs if j["state"] == "error")},
            "emulator": self._emulator_view(resolved, cfg),
            "retroarch": self._retroarch_view(),
            "dolphin": self._dolphin_view(),
            "saves": {"enabled": s.get("saves.enabled"), "conflicts": conflicts, "card_type": s.get("saves.card_type")},
            "helper": self._helper_view(),
        }

    def _conflict_of(self, rom_id: int) -> dict | None:
        rec = savesmod.Record(rom_id)
        c = rec.doc.get("conflict")
        return c if c else None

    def _retroarch_view(self) -> dict:
        """What the screens say about RetroArch: whether it is there, where its saves go, and how many installed games use it."""
        s = self.settings
        r = libretro.resolve(s.get("emulators.retroarch.data_dir"), s.get("emulators.retroarch.save_dir"),
                             s.get("emulators.retroarch.executable"))
        c = r.config
        games = sum(1 for g in self.manifest.all() if emulators.group_for_platform(g.get("platform_slug", "")) == "retroarch")
        return {"name": r.profile.name, "data_dir": str(r.data_dir), "data_dir_exists": r.data_dir_exists, "found_by": c.why,
                "executable": r.executable, "present": bool(r.executable or c.cfg_exists), "games": games,
                "save_dir": str(c.save_dir) if c.save_dir else "", "next_to_game": c.save_dir is None,
                "sort_by_core": c.sort_by_core, "sort_by_content": c.sort_by_content,
                "running": bool(emulators.find_instances(r.profile.process_names, self.proc_root))}

    def _dolphin_view(self) -> dict:
        s = self.settings
        r = emulators.resolve_dolphin(s.get("emulators.dolphin.data_dir"), s.get("emulators.dolphin.executable"))
        games = sum(1 for g in self.manifest.all() if emulators.group_for_platform(g.get("platform_slug", "")) == "dolphin")
        opts = emulators.dolphin_options(r.data_dir)
        return {"name": r.profile.name, "data_dir": str(r.data_dir), "data_dir_exists": r.data_dir_exists, "found_by": r.why,
                "executable": r.executable, "present": bool(r.executable or r.data_dir_exists), "games": games,
                "raw_card": opts.get("SlotA") == "1", "running": bool(emulators.find_instances(r.profile.process_names, self.proc_root))}

    def _emulator_view(self, r: emulators.Resolved, cfg: emulators.CardConfig) -> dict:
        s1 = cfg.slots[0]
        return {"name": r.profile.name, "id": r.profile.id, "data_dir": str(r.data_dir), "data_dir_exists": r.data_dir_exists,
                "found_by": r.why, "executable": r.executable, "running": bool(emulators.find_instances(
                    emulators.all_ps2_process_names())),
                "card": {"slot1": {"name": s1.name, "kind": s1.kind, "enabled": s1.enabled},
                         "wanted": self.settings.get("saves.card_type"), "custom_dir": cfg.custom_dir},
                "bios": self._bios_summary(r)}

    @staticmethod
    def _bios_summary(r: emulators.Resolved) -> dict:
        folder = emulators.bios_dir(r.data_dir)
        count = sum(1 for f in emulators.list_bios(folder)["files"] if f["kind"] == "bios")
        return {"dir": str(folder), "has_bios": count > 0, "count": count}

    def _helper_view(self) -> dict:
        try:
            v = self.bridge.version()
            return {"ok": True, "arch": v.get("arch"), "helper": v.get("helper"), "sigil": v.get("sigil_pin")}
        except SavebridgeError as e:
            return {"ok": False, "error": str(e)}

    # -- settings ------------------------------------------------------------------------------------------

    def get_settings(self) -> dict:
        return self.settings.as_dict()

    def update_settings(self, patch: dict) -> dict:
        changed, errors = self.settings.update(patch)
        if "library" in changed:
            self._lists.clear()
        if errors:
            return {"ok": False, "errors": errors, "settings": self.settings.as_dict()}
        if changed.get("library", {}).get("remote_installs") is not None and self.settings.signed_in:
            try:  # tell RomM whether this device takes games sent from its web UI
                self.client().update_device(self.settings.get("auth.device_id"),
                                            capabilities={"remote_install": bool(self.settings.get("library.remote_installs"))})
            except RommError as e:
                log.debug("could not update the device's capabilities: %s", e)
        if changed.get("saves") or changed.get("library", {}).get("remote_installs") is not None:
            self.watcher.poke()
        return {"ok": True, "changed": changed, "settings": self.settings.as_dict()}

    def check_location(self, path: str) -> dict:
        """Can downloads go here? Used by the location picker before the setting is changed."""
        from .config import expand_path
        p = Path(expand_path(path or ""))
        if not path or not p.is_absolute():
            return {"ok": False, "message": "Enter a full path, like /run/media/mmcblk0p1/ROMs"}
        probe = p
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        writable = os.access(probe, os.W_OK)
        vol = volume_info(probe)
        return {"ok": writable, "path": str(p), "exists": p.exists(), "free": vol["free"], "total": vol["total"],
                "message": "" if writable else f"{probe} is not writable by this user"}

    def volumes(self) -> list[dict]:
        out = [{"path": str(paths.user_home() / "ROMs"), "name": "Home folder", **volume_info(paths.user_home())}]
        out += removable_volumes()
        return out

    # -- library -------------------------------------------------------------------------------------------------

    def _cached(self, key: tuple, ttl: float, fetch: Callable[[], Any]) -> Any:
        now = time.monotonic()
        hit = self._lists.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
        value = fetch()
        self._lists[key] = (now, value)
        return value

    def platforms(self) -> dict:
        cache = paths.cache_dir() / "platforms.json"
        installed_by_platform: dict[Any, int] = {}
        for g in self.manifest.all():
            installed_by_platform[g.get("platform_id")] = installed_by_platform.get(g.get("platform_id"), 0) + 1
        stale = False
        try:
            raw = self._cached(("platforms",), LIST_CACHE_TTL, self.client().platforms)
            slim = [{"id": p["id"], "name": p.get("display_name") or p.get("custom_name") or p.get("name"),
                     "slug": p.get("slug"), "fs_slug": p.get("fs_slug"), "rom_count": p.get("rom_count", 0)} for p in raw]
            write_json(cache, {"at": iso(), "platforms": slim})
        except RommError as e:
            doc = read_json(cache, {})
            slim = doc.get("platforms", []) if isinstance(doc, dict) else []
            stale = True
            if not slim:
                # nothing cached: at least show what is installed
                seen = {}
                for g in self.manifest.all():
                    seen[g.get("platform_id")] = {"id": g.get("platform_id"), "name": g.get("platform_name") or g.get("platform_slug"),
                                                  "slug": g.get("platform_slug"), "fs_slug": g.get("platform_fs_slug"), "rom_count": 0}
                slim = list(seen.values())
            if e.kind == "auth":
                self.emit("auth_required", {})
        lay = self.layout()
        for p in slim:
            p["folder"] = lib.platform_dir_name(lay.style, lay.overrides, p.get("slug", ""), p.get("fs_slug", ""))
            p["installed"] = installed_by_platform.get(p["id"], 0)
            p["saves"] = emulators.group_for_platform(p.get("slug", "")) is not None
        slim.sort(key=lambda p: (p["name"] or "").lower())
        return {"platforms": slim, "stale": stale}

    def _slim_rom(self, rom: dict, client: RommClient | None) -> dict:
        rid = rom["id"]
        inst = self.manifest.get(rid)
        job = self.downloads.get(rid)
        return {"id": rid, "name": rom.get("name") or rom.get("fs_name"), "fs_name": rom.get("fs_name"),
                "size": rom.get("fs_size_bytes") or 0, "platform_id": rom.get("platform_id"),
                "platform_slug": rom.get("platform_slug"), "regions": rom.get("regions") or [],
                "cover": "",
                "installed": bool(inst), "download": job}

    def roms(self, platform_id: int | None, search: str = "", offset: int = 0, limit: int = 40) -> dict:
        search = (search or "").strip()
        try:
            client = self.client()
            page = self._cached(("roms", platform_id, search, offset, limit), 20.0,
                                lambda: client.roms(platform_id, search, limit, offset))
            items = [self._slim_rom(r, client) for r in page.get("items", [])]
            return {"items": items, "total": page.get("total", len(items)), "offset": offset, "stale": False}
        except RommError as e:
            if e.kind == "auth":
                self.emit("auth_required", {})
            out = self.installed_games(search, offset, limit, platform_id)
            out.update(stale=True, message=describe(e))
            return out

    def installed_games(self, search: str = "", offset: int = 0, limit: int = 40, platform_id: int | None = None) -> dict:
        """The games the plugin installed, from the manifest alone (no server needed)."""
        search = (search or "").strip().lower()
        games = [g for g in self.manifest.all() if platform_id in (None, g.get("platform_id"))
                 and search in (g.get("name") or "").lower()]
        games.sort(key=lambda g: (g.get("name") or "").lower())
        items = [{"id": g["rom_id"], "name": g["name"], "fs_name": g.get("fs_name"), "size": g.get("size", 0),
                  "platform_id": g.get("platform_id"), "platform_slug": g.get("platform_slug"), "regions": [], "cover": "",
                  "installed": True, "download": self.downloads.get(g["rom_id"])} for g in games]
        return {"items": items[offset:offset + limit], "total": len(items), "offset": offset, "stale": False}

    def _cover_data(self, client: RommClient, rom: dict) -> str:
        """The cover as a data: URI. Steam's UI is served over https and would refuse an http image from a RomM on the
        LAN, so the plugin fetches it and hands over the bytes. Cached; never raises."""
        path = rom.get("path_cover_small") or rom.get("path_cover_large") or ""
        if not path or path.startswith("http"):
            return ""
        if not path.startswith("/"):
            path = "/assets/romm/resources/" + path
        cache = paths.cache_dir() / "covers" / f"{rom.get('id')}.json"
        doc = read_json(cache, None)
        if isinstance(doc, dict) and doc.get("path") == path and doc.get("uri"):
            return doc["uri"]
        try:
            r = client.http.request("GET", path, headers={"Accept": "image/*"}, timeout=10, retries=0)
        except Exception as e:  # noqa: BLE001 - a missing cover is not worth an error
            log.debug("cover of %s not fetched: %s", rom.get("id"), e)
            return ""
        mime = (r.headers.get("content-type") or "image/webp").split(";")[0].strip()
        if not mime.startswith("image/") or not r.body or len(r.body) > 2 * 1024 * 1024:
            return ""
        import base64
        uri = f"data:{mime};base64,{base64.b64encode(r.body).decode('ascii')}"
        try:
            write_json(cache, {"path": path, "uri": uri})
        except OSError:
            pass
        return uri

    def rom(self, rom_id: int) -> dict:
        inst = self.manifest.get(rom_id)
        client = None
        detail: dict | None = None
        offline = ""
        try:
            client = self.client()
            detail = client.rom(rom_id)
        except RommError as e:
            offline = describe(e)
        if detail is None and not inst:
            raise RommError("not_found", "That game is not available right now" + (f": {offline}" if offline else ""))
        d = detail or {}
        files = [{"name": f.get("file_name"), "size": f.get("file_size_bytes"), "category": f.get("category") or "game"}
                 for f in (d.get("files") or [])]
        meta = d.get("metadatum") or {}
        slug = d.get("platform_slug") or (inst or {}).get("platform_slug") or ""
        group = emulators.group_for_platform(slug)
        out = {
            "id": rom_id, "name": d.get("name") or (inst or {}).get("name"), "fs_name": d.get("fs_name"),
            "platform": d.get("platform_display_name") or (inst or {}).get("platform_name"), "platform_slug": slug,
            "summary": d.get("summary") or "", "size": d.get("fs_size_bytes") or (inst or {}).get("size", 0),
            "regions": d.get("regions") or [], "languages": d.get("languages") or [], "genres": meta.get("genres") or [],
            "companies": meta.get("companies") or [], "files": files,
            "cover": self._cover_data(client, d) if client and d else "",
            "installed": bool(inst), "local": ({"main_file": inst.get("main_file"), "path": inst.get("top"), "size": inst.get("size"),
                                                "installed_at": inst.get("installed_at")} if inst else None),
            "download": self.downloads.get(rom_id), "offline": offline,
            "saves": self.saves.status(rom_id) if inst else {"supported": group is not None},
            "saves_supported": group is not None,
        }
        return out

    # -- installing ----------------------------------------------------------------------------------------------

    def install(self, rom_id: int) -> dict:
        rom = self.client().rom(rom_id)
        return self.downloads.enqueue(rom)

    def uninstall(self, rom_id: int) -> dict:
        game = self.manifest.get(rom_id)
        if game:
            for inst in emulators.find_instances(emulators.all_process_names(), self.proc_root):
                # on its command line, or (started from the emulator's own menu) the disc image it holds open
                held = ([inst.rom_path()] if inst.rom_path() else []) + inst.open_files()
                if any(g["rom_id"] == rom_id for _, g in self.manifest.by_paths(held)):
                    return {"ok": False, "message": "The game is running"}
        out = self.downloads.uninstall(rom_id)
        return {"ok": True, **out}

    def _on_installed(self, entry: dict) -> None:
        """After a download: read the disc's id, make sure the memory card is ready, pick up the save from RomM."""
        if not entry:
            return
        rid = entry["rom_id"]
        self.emit("installed", {"rom_id": rid, "name": entry.get("name")})

        def work() -> None:
            try:
                if emulators.group_for_platform(entry.get("platform_slug", "")) and entry.get("main_file"):
                    if savesmod.emulators_platform(entry):          # a PS2 or GameCube disc: sigil reads its id (RetroArch games need none)
                        ident = self.bridge.identify(entry["main_file"], savesmod.emulators_platform(entry))
                        self.manifest.update(rid, identity=ident, title_id=ident.get("title_id"))
                    if self.settings.get("saves.enabled") and self.settings.get("saves.pull_when_idle"):
                        self.saves.sync([rid], trigger="install")
            except (SigilRefusal, SavebridgeError) as e:
                log.warn("could not read the disc of %s: %s", entry.get("name"), e)
            except Exception:  # noqa: BLE001
                log.exception("after-install work failed")

        threading.Thread(target=work, name="romm-after-install", daemon=True).start()

    # -- saves ---------------------------------------------------------------------------------------------------

    def sync_game(self, rom_id: int, resolution: str | None = None) -> dict:
        res = self.saves.sync([rom_id], resolutions={rom_id: resolution} if resolution else None)
        return res[0] if res else {"status": "error", "message": "Nothing to sync"}

    def sync_all(self) -> list[dict]:
        ids = [g["rom_id"] for g in self.manifest.all() if emulators.group_for_platform(g.get("platform_slug", ""))]
        return self.saves.sync(ids, trigger="manual") if ids else []

    def setup_cards(self, why: str = "") -> None:
        """Makes memory card slot 1 a folder card before a game is ever played, so the saves are folder saves from the first one.

        There is no setting for it to change: the emulator takes a card for a folder card when what is at its path is a
        directory, so the card is created there, or an empty file card the emulator made on its first start is replaced.
        Quiet, and only when the emulator is installed or has run; never while it runs, never for a card that holds saves."""
        s = self.settings
        if not s.get("saves.enabled") or s.get("saves.card_type") != "folder":
            return
        try:
            r = emulators.resolve_ps2(s.get("emulators.ps2.emulator"), s.get("emulators.ps2.data_dir"), s.get("emulators.ps2.executable"))
            if not (r.data_dir_exists or r.executable) or self._ps2_running():
                return
            _, note = self.saves.prepare_cards(r, None)
        except (SavebridgeError, SigilRefusal, OSError) as e:
            log.debug("memory card not set up (%s): %s", why, e)
            return
        if note.get("state") == "blocked":
            log.info("memory card left as it is (%s): %s", why, note.get("message"))

    def prepare_cards(self) -> dict:
        """Settings > Saves > 'Set up the memory card': makes the card the kind the setting asks for."""
        s = self.settings
        r = emulators.resolve_ps2(s.get("emulators.ps2.emulator"), s.get("emulators.ps2.data_dir"), s.get("emulators.ps2.executable"))
        if any(emulators.find_instances(emulators.all_ps2_process_names(), self.proc_root)):
            return {"ok": False, "message": "Close the emulator first"}
        try:
            cfg, note = self.saves.prepare_cards(r, None)
        except (SavebridgeError, SigilRefusal, OSError) as e:
            return {"ok": False, "message": str(e)}
        ok = note.get("state") == "ok"
        msg = note.get("message") or ("Memory card ready: " + ("created a folder card" if note.get("created") else
                                      "replaced the empty file card with a folder card" if note.get("replaced_empty") else
                                      f"{note.get('kind', 'card')} card"))
        return {"ok": ok, "message": msg, "state": note, "emulator": self._emulator_view(r, cfg)}

    def set_game_sync(self, rom_id: int, enabled: bool) -> dict:
        self.saves.set_enabled(rom_id, enabled)
        return {"ok": True}

    def forget_game_sync(self, rom_id: int) -> dict:
        self.saves.forget(rom_id)
        return {"ok": True}

    def conflicts(self) -> list[dict]:
        out = []
        for g in self.manifest.all():
            c = self._conflict_of(g["rom_id"])
            if c:
                out.append(c)
        return out

    def backups(self, rom_id: int) -> list[dict]:
        return savesmod.list_backups(rom_id)

    def restore_backup(self, rom_id: int, name: str) -> dict:
        """Puts a backed-up memory card back. The current card is backed up first."""
        if any(emulators.find_instances(emulators.all_ps2_process_names(), self.proc_root)):
            return {"ok": False, "message": "Close the emulator first"}
        src = paths.backups_dir() / f"game-{rom_id}" / name
        if not src.is_dir() or "/" in name or name.startswith("."):
            return {"ok": False, "message": "That backup is not there any more"}
        game = self.manifest.get(rom_id) or {}
        s = self.settings
        r = emulators.resolve_ps2(s.get("emulators.ps2.emulator"), s.get("emulators.ps2.data_dir"), s.get("emulators.ps2.executable"))
        cfg = emulators.card_config(r.data_dir, (game.get("identity") or {}).get("title_id"))
        if cfg.custom_dir:
            return {"ok": False, "message": "The memory cards are outside the emulator's data folder"}
        savesmod.backup_cards([sl.path for sl in cfg.slots if sl.enabled], rom_id, "before-backup-restore", s.get("saves.backup_keep"))
        restored = []
        for item in sorted(src.iterdir()):
            if item.name.startswith("."):
                continue
            dest = cfg.memcards_dir / item.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.is_dir():
                shutil.rmtree(dest)
            elif dest.exists():
                dest.unlink()
            if item.is_dir():
                shutil.copytree(item, dest, symlinks=True)
            else:
                shutil.copy2(item, dest)
            restored.append(item.name)
        savesmod.Record(rom_id).reset()   # what sigil remembered no longer describes the card
        return {"ok": True, "restored": restored, "message": f"Restored {', '.join(restored) or 'nothing'}"}

    def _saves_view(self) -> dict:
        """How saves are kept on the server: what the setting says, and what the server last said (remembered, no request)."""
        s = self.settings
        known = read_json(paths.state_dir() / "server.json", {})
        known = known if isinstance(known, dict) and known.get("url") == s.get("server.url") else {}
        wanted = s.get("saves.format")
        using = wanted if wanted != "auto" else ("card" if known.get("units") else "folders")
        return {"slot": s.get("saves.slot"), "tag": savesmod.EMULATOR_TAG, "format": wanted, "using": using,
                "server_version": known.get("version") or "", "server_units": bool(known.get("units"))}

    # -- BIOS files ----------------------------------------------------------------------------------------------

    def _ps2(self) -> tuple[emulators.Resolved, Path]:
        s = self.settings
        r = emulators.resolve_ps2(s.get("emulators.ps2.emulator"), s.get("emulators.ps2.data_dir"), s.get("emulators.ps2.executable"))
        return r, emulators.bios_dir(r.data_dir)

    def _ps2_running(self) -> bool:
        return any(emulators.find_instances(emulators.all_ps2_process_names(), self.proc_root))

    def _bios_remote(self, r: emulators.Resolved, folder: Path) -> tuple[dict, list[dict]]:
        """What RomM offers for the emulator's platform: ({ok, platform, items, reason, message}, RomM's own records)."""
        platform: dict | None = None
        try:
            client = self.client()
            for p in self._cached(("platforms",), LIST_CACHE_TTL, client.platforms):
                if (p.get("slug") or "").lower() in r.profile.platform_slugs:
                    platform = {"id": p["id"], "name": p.get("display_name") or p.get("custom_name") or p.get("name") or "PlayStation 2"}
                    break
            if platform is None:
                return ({"ok": True, "platform": None, "items": [], "reason": "no_platform",
                         "message": "RomM has no PlayStation 2 library yet, so it has no BIOS files for it."}, [])
            raw = client.firmware(platform["id"])
        except RommError as e:
            if e.kind == "auth":
                self.emit("auth_required", {})
            text = ("This device's sign-in does not include firmware access (the permission firmware.read). To fix it, "
                    "pair again and allow it when RomM asks." if e.kind == "forbidden" else describe(e))
            return {"ok": False, "platform": platform, "items": [], "reason": e.kind, "message": text}, []
        items = bios.describe_items(raw, folder)
        empty = ("" if items else f"RomM has no firmware for {platform['name']}. Add the BIOS in RomM (open the platform, "
                                  "then Firmware), or put the file in the folder below yourself.")
        return {"ok": True, "platform": platform, "items": items, "reason": "" if items else "empty", "message": empty}, raw

    def bios_status(self, remote: bool = True) -> dict:
        """The emulator's BIOS folder as it is, and with `remote` what RomM offers for it."""
        r, folder = self._ps2()
        listing = emulators.list_bios(folder)
        return {"emulator": {"name": r.profile.name, "id": r.profile.id, "running": self._ps2_running()},
                "dir": str(folder), "dir_exists": folder.is_dir(), "local": listing["files"], "others": listing["others"],
                "has_bios": any(f["kind"] == "bios" for f in listing["files"]),
                "remote": self._bios_remote(r, folder)[0] if remote else None}

    def bios_install(self, ids: list[int] | None = None, overwrite: bool = False) -> dict:
        """Downloads RomM's BIOS files into the emulator's folder. ids: only these (by RomM's firmware ids); None = every
        file the folder does not have. overwrite replaces a different file of the same name, only for the given ids."""
        if not self._bios_lock.acquire(blocking=False):
            return {"ok": False, "message": "BIOS files are already being installed"}
        try:
            r, folder = self._ps2()
            remote, raw = self._bios_remote(r, folder)
            if not remote["ok"]:
                return {"ok": False, "error": remote["reason"], "message": remote["message"]}
            if not raw:
                return {"ok": False, "error": remote["reason"] or "empty", "message": remote["message"], "status": self.bios_status(False)}
            if ids is not None:
                chosen = [fw for fw in raw if fw["id"] in {int(i) for i in ids}]
                if not chosen:
                    return {"ok": False, "error": "gone", "message": "RomM does not list those files any more. Refresh the list."}
            else:
                chosen = raw
            try:
                report = bios.install(self.client(), chosen, folder, overwrite=bool(overwrite) and ids is not None,
                                      emulator_running=self._ps2_running(), backup_root=paths.backups_dir() / "bios")
            except OSError as e:
                return {"ok": False, "error": "space" if e.errno == 28 else "disk", "message": e.strerror or str(e)}
            status = self.bios_status(False)
            status["remote"] = {**remote, "items": bios.describe_items(raw, folder)}
            return {"ok": not (report["failed"] or report["busy"]), "report": report, "dir": str(folder),
                    "message": bios.summarize(report, folder), "status": status}
        finally:
            self._bios_lock.release()

    # -- connecting -------------------------------------------------------------------------------------------------

    def disconnect(self) -> dict:
        self.connector.sign_out()
        self._client = None
        self._lists.clear()
        return {"ok": True}

    def after_connect(self) -> None:
        self._client = None
        self._lists.clear()
        self.watcher.poke()
        threading.Thread(target=self.setup_cards, args=("connect",), name="romm-cards", daemon=True).start()

    # -- diagnostics ---------------------------------------------------------------------------------------------------

    def diagnostics(self) -> dict:
        s = self.settings
        r = emulators.resolve_ps2(s.get("emulators.ps2.emulator"), s.get("emulators.ps2.data_dir"), s.get("emulators.ps2.executable"))
        helper = self._helper_view()
        return {
            "version": PLUGIN_VERSION, "helper": helper, "helper_tried": list(self.bridge.tried),
            "host": {"machine": os.uname().machine, "kernel": os.uname().release, "user": os.environ.get("USER", ""),
                     "home": str(paths.user_home())},
            "paths": {"settings": str(paths.settings_dir()), "state": str(paths.state_dir()), "logs": str(paths.log_dir()),
                      "backups": str(paths.backups_dir())},
            "emulator": {"name": r.profile.name, "data_dir": str(r.data_dir), "ini": str(r.ini), "ini_exists": r.ini.exists(),
                         "executable": r.executable, "found_by": r.why},
            "retroarch": self._retroarch_view(),
            "dolphin": self._dolphin_view(),
            "library_free": human_bytes(free_bytes(s.get("library.root"))),
            "saves": self._saves_view(),
        }
