# SPDX-License-Identifier: MIT
"""Save sync for the games the plugin installed.

For one game, one slot (the `autosave` slot, as Argosy and RomM's web player use):

    collect the game's saves from the emulator's memory card (sigil)  ->  tell RomM what this device holds
    (POST /api/sync/negotiate)  ->  RomM answers upload, download, conflict or nothing  ->  do that.

Rules that keep a card safe:
  * never touch a card while the emulator runs (collect and restore both wait);
  * a card is backed up before anything is written to it;
  * a save is never replaced on a guess: when this device has never synced the game and both sides hold
    different saves, the user decides (or the policy they set does);
  * the PS2 card is a folder card by default (Settings > Saves > Memory card), so a restore writes the game's
    save folders and nothing else, and the unit RomM stores is the same either way.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import emulators, log, paths
from .config import Settings
from .library import Manifest
from .romm import RommClient, RommError, describe
from .sigil import Savebridge, SavebridgeError, SigilRefusal
from .util import iso, now_utc, read_json, ts_to_iso, write_json

HISTORY_LIMIT = 20


# --- result helpers ---------------------------------------------------------------------------------

def result(rom_id: int, name: str, status: str, message: str, **extra: Any) -> dict:
    return {"rom_id": rom_id, "name": name, "status": status, "message": message, **extra}


def sync_dir() -> Path:
    return paths.state_dir() / "sync"


# --- per-game record ------------------------------------------------------------------------------


class Record:
    """What this device remembers about a game's sync: the last agreement, a pending conflict, a short history."""

    def __init__(self, rom_id: int) -> None:
        self.rom_id = rom_id
        self.path = sync_dir() / f"{rom_id}.json"
        self.state_path = sync_dir() / f"{rom_id}.sigil"
        doc = read_json(self.path, None)
        self.doc: dict = doc if isinstance(doc, dict) else {"rom_id": rom_id, "enabled": True, "last": None,
                                                             "conflict": None, "history": []}

    @property
    def last(self) -> dict | None:
        return self.doc.get("last")

    @property
    def enabled(self) -> bool:
        return bool(self.doc.get("enabled", True))

    def save(self) -> None:
        write_json(self.path, self.doc)

    def note(self, action: str, detail: str) -> None:
        h = self.doc.setdefault("history", [])
        h.append({"at": iso(), "action": action, "detail": detail})
        del h[:-HISTORY_LIMIT]

    def commit_state(self, tmp: Path | None) -> None:
        """sigil's state, kept only once what it describes has happened."""
        if not tmp or not Path(tmp).exists():
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(tmp, self.state_path)
        except OSError:  # another filesystem: copy beside the target, then swap
            side = self.state_path.with_name(self.state_path.name + ".new")
            shutil.copyfile(tmp, side)
            os.replace(side, self.state_path)

    def reset(self) -> None:
        for p in (self.path, self.state_path):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
        self.doc = {"rom_id": self.rom_id, "enabled": True, "last": None, "conflict": None, "history": []}


# --- backups -------------------------------------------------------------------------------------------


def _fingerprint(path: Path) -> str:
    h = hashlib.sha1()
    if path.is_file():
        st = path.stat()
        h.update(f"{st.st_size}:{st.st_mtime_ns}".encode())
    elif path.is_dir():
        for root, dirs, files in os.walk(path):
            dirs.sort()
            for f in sorted(files):
                p = Path(root) / f
                try:
                    st = p.stat()
                except OSError:
                    continue
                h.update(f"{p.relative_to(path)}:{st.st_size}:{st.st_mtime_ns}\n".encode())
    return h.hexdigest()


def backup_cards(cards: list[Path], rom_id: int, reason: str, keep: int) -> Path | None:
    """Copies the cards (files or folders) aside before they are written. Skips when nothing changed since
    the last backup of the same cards, and keeps only the newest `keep`."""
    cards = [c for c in cards if c.exists()]
    if not cards:
        return None
    root = paths.backups_dir() / f"game-{rom_id}"
    fp = hashlib.sha1("".join(f"{c.name}:{_fingerprint(c)}" for c in cards).encode()).hexdigest()
    existing = sorted(p for p in root.glob("*") if p.is_dir()) if root.exists() else []
    if existing and (existing[-1] / ".fingerprint").is_file() and (existing[-1] / ".fingerprint").read_text() == fp:
        return existing[-1]
    stamp = time.strftime("%Y%m%d-%H%M%S") + f"{int(time.time() * 1000) % 1000:03d}"
    dest = root / (stamp + "-" + reason)
    n = 1
    while dest.exists():
        n += 1
        dest = root / f"{stamp}-{n}-{reason}"
    dest.mkdir(parents=True)
    for c in cards:
        if c.is_dir():
            shutil.copytree(c, dest / c.name, symlinks=True)
        else:
            shutil.copy2(c, dest / c.name)
    (dest / ".fingerprint").write_text(fp)
    existing = sorted(p for p in root.glob("*") if p.is_dir())
    for old in existing[:-keep]:
        shutil.rmtree(old, ignore_errors=True)
    return dest


def list_backups(rom_id: int) -> list[dict]:
    root = paths.backups_dir() / f"game-{rom_id}"
    out = []
    if root.exists():
        for p in sorted(root.glob("*"), reverse=True):
            if p.is_dir():
                size = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
                out.append({"name": p.name, "path": str(p), "size": size})
    return out


# --- the emulator context for a game -------------------------------------------------------------------


@dataclass
class Ctx:
    profile: emulators.Profile
    data_dir: Path
    cfg: emulators.CardConfig
    options: dict
    executable: str
    card: dict          # what prepare_cards found or did

    @property
    def root(self) -> str:
        return str(self.data_dir)


def local_mtime(cfg: emulators.CardConfig, save_id: str) -> float:
    """When this game's saves were last written, as far as the files say."""
    best = 0.0
    for s in cfg.slots:
        if not s.enabled:
            continue
        try:
            if s.kind == "file":
                best = max(best, s.path.stat().st_mtime)
            elif s.kind == "folder" and save_id:
                for d in s.path.iterdir():
                    if d.is_dir() and d.name.startswith(save_id):
                        for root, _dirs, files in os.walk(d):
                            for f in files:
                                best = max(best, os.stat(os.path.join(root, f)).st_mtime)
        except OSError:
            continue
    return best


class SaveSync:
    def __init__(self, settings: Settings, manifest: Manifest, get_client: Callable[[], RommClient],
                 bridge: Savebridge, emit: Callable[[str, dict], None] | None = None,
                 reregister: Callable[[], str] | None = None, proc_root: str = "/proc") -> None:
        self.proc_root = proc_root
        self.settings = settings
        self.manifest = manifest
        self.get_client = get_client
        self.bridge = bridge
        self.emit = emit or (lambda event, payload: None)
        self.reregister = reregister
        self._lock = threading.RLock()

    # -- context ----------------------------------------------------------------------------------------

    def _emulator_for(self, game: dict) -> tuple[emulators.Resolved, str] | None:
        if emulators.group_for_platform(game.get("platform_slug", "")) != "ps2":
            return None
        s = self.settings
        r = emulators.resolve_ps2(s.get("emulators.ps2.emulator"), s.get("emulators.ps2.data_dir"),
                                  s.get("emulators.ps2.executable"))
        return r, "ps2"

    def emulator_running(self, game: dict | None = None) -> bool:
        return bool(emulators.find_instances(emulators.all_ps2_process_names(), self.proc_root))

    def prepare_cards(self, r: emulators.Resolved, serial: str | None) -> tuple[emulators.CardConfig, dict]:
        """Makes sure the memory card is the kind the settings ask for. Returns the card config and a note."""
        policy = self.settings.get("saves.card_type")
        cfg = emulators.card_config(r.data_dir, serial)
        s1 = cfg.slots[0]
        if not s1.enabled:
            return cfg, {"state": "disabled", "message": "Memory card slot 1 is turned off in the emulator"}
        if cfg.custom_dir:
            return cfg, {"state": "blocked", "reason": "custom_dir",
                         "message": "The emulator keeps its memory cards outside its data folder, which "
                                    "the save helper cannot follow. Move them back to <data folder>/memcards"}
        if policy != "folder":
            return cfg, {"state": "ok", "kind": s1.kind}
        root = str(r.data_dir)
        if s1.kind == "folder":
            try:
                got = self.bridge.init_folder_card(root, s1.name)
            except SavebridgeError:
                got = {"status": "exists"}
            if got.get("status") == "unformatted_with_saves":
                return cfg, {"state": "blocked", "reason": "damaged_folder_card",
                             "message": f"The folder card {s1.name} holds saves but has no valid superblock. "
                                        "Open it once in the emulator, or restore a backup"}
            return emulators.card_config(r.data_dir, serial), {"state": "ok", "kind": "folder"}
        if s1.kind == "missing":
            self.bridge.init_folder_card(root, s1.name)
            log.info("created folder card %s", s1.path)
            return emulators.card_config(r.data_dir, serial), {"state": "ok", "kind": "folder", "created": True}
        if s1.kind == "file":
            saves = self._saves_on_file_card(s1.path)
            if saves == 0:
                aside = s1.path.with_name(f"{s1.name}.empty-{time.strftime('%Y%m%d-%H%M%S')}.bak")
                os.replace(s1.path, aside)
                self.bridge.init_folder_card(root, s1.name)
                log.info("replaced the empty file card %s with a folder card (old one kept as %s)", s1.path, aside.name)
                return emulators.card_config(r.data_dir, serial), {"state": "ok", "kind": "folder", "replaced_empty": True}
            return cfg, {"state": "blocked", "reason": "file_card",
                         "message": f"Memory card slot 1 ({s1.name}) is a file card with {saves if saves > 0 else 'some'} saves. "
                                    "Folder cards are needed: convert it in the emulator (Settings, Memory Cards, "
                                    "Convert), or set Memory card to 'Use what the emulator has'"}
        return cfg, {"state": "blocked", "reason": "unusable",
                     "message": f"{s1.path} is not a memory card the plugin can use"}

    def _saves_on_file_card(self, path: Path) -> int:
        """How many saves a file card holds; 0 for an unformatted or empty card, -1 when it cannot be read."""
        try:
            return len(self.bridge.card_list(str(path)).get("entries", []))
        except SigilRefusal as e:
            return 0 if e.name == "unsupported_format" else -1
        except SavebridgeError:
            return -1

    def context(self, game: dict) -> Ctx | dict:
        """The emulator context for a game, or a result dict that says why there is none."""
        rid, name = game["rom_id"], game.get("name", "")
        got = self._emulator_for(game)
        if not got:
            return result(rid, name, "unsupported", "Save sync is not set up for this system yet")
        r, _ = got
        serial = (game.get("identity") or {}).get("title_id") or game.get("title_id")
        try:
            cfg, card = self.prepare_cards(r, serial)
        except (SavebridgeError, SigilRefusal, OSError) as e:
            return result(rid, name, "error", f"Could not prepare the memory card: {e}")
        if card.get("state") == "disabled":
            return result(rid, name, "unsupported", card["message"])
        if card.get("state") == "blocked":
            return result(rid, name, "blocked", card["message"], reason=card.get("reason"))
        return Ctx(r.profile, r.data_dir, cfg, emulators.sigil_options(cfg), r.executable, card)

    # -- identity ----------------------------------------------------------------------------------------

    def identity(self, game: dict) -> dict:
        """sigil's reading of the disc, from the manifest or (once) from the file."""
        ident = game.get("identity")
        if ident and ident.get("save_id"):
            return ident
        main = game.get("main_file")
        slug = emulators_platform(game)
        ident = self.bridge.identify(main, slug)
        self.manifest.update(game["rom_id"], identity=ident, title_id=ident.get("title_id"))
        return ident

    # -- the pieces -------------------------------------------------------------------------------------------

    def _collect(self, ctx: Ctx, game: dict, rec: Record, work: Path) -> dict:
        ident = self.identity(game)
        game = {**game, "identity": ident}
        work.mkdir(parents=True, exist_ok=True)
        out = work / "unit.bin"
        state_out = work / "state.collect"
        got = self.bridge.collect(
            layout=ctx.profile.layout, root=ctx.root, out=out, rom=game.get("main_file"), identity=ident,
            platform_slug=ctx.profile.sigil_platform, content=game.get("main_file"), options=ctx.options,
            game_ids=[ident.get("title_id")] if ident.get("title_id") else None,
            state_in=rec.state_path, state_out=state_out)
        got["unit_path"] = str(out) if got.get("has_unit") else ""
        got["state_tmp"] = str(state_out) if got.get("has_state") else ""
        got["mtime"] = local_mtime(ctx.cfg, ident.get("save_id", ""))
        return got

    def _client_state(self, ctx: Ctx, game: dict, local: dict) -> dict:
        size = Path(local["unit_path"]).stat().st_size if local.get("unit_path") else 0
        return {"rom_id": game["rom_id"], "file_name": local["artifact"], "slot": self.settings.get("saves.slot"),
                "emulator": ctx.profile.romm_emulator, "content_hash": local["content_hash"],
                "updated_at": ts_to_iso(local["mtime"] or time.time()), "file_size_bytes": size}

    # -- one or many games ----------------------------------------------------------------------------------

    def sync(self, rom_ids: list[int], *, trigger: str = "manual", resolutions: dict[int, str] | None = None,
             only_if_changed: bool = False) -> list[dict]:
        """Brings each game's save in line with RomM. `resolutions` answers pending conflicts: rom id ->
        'server' | 'device' | 'keep_both'."""
        with self._lock:
            return self._sync(rom_ids, trigger, resolutions or {}, only_if_changed)

    def _sync(self, rom_ids: list[int], trigger: str, resolutions: dict[int, str], only_if_changed: bool) -> list[dict]:
        s = self.settings
        results: dict[int, dict] = {}
        if not s.get("saves.enabled"):
            return [result(r, "", "disabled", "Save sync is turned off") for r in rom_ids]
        if not s.signed_in or not s.get("auth.device_id"):
            return [result(r, "", "auth", "Connect to RomM first") for r in rom_ids]
        work = paths.runtime_dir() / "work" / f"sync-{os.getpid()}-{int(time.time() * 1000)}"
        work.mkdir(parents=True, exist_ok=True)
        plans: dict[int, dict] = {}
        try:
            for rid in rom_ids:
                game = self.manifest.get(rid)
                if not game:
                    results[rid] = result(rid, "", "not_installed", "This game is not installed by the plugin")
                    continue
                rec = Record(rid)
                if not rec.enabled and trigger != "manual":
                    results[rid] = result(rid, game["name"], "paused", "Sync is paused for this game")
                    continue
                if self.emulator_running(game):
                    results[rid] = result(rid, game["name"], "busy", "The emulator is running; the save is synced after it closes")
                    continue
                ctx = self.context(game)
                if isinstance(ctx, dict):
                    results[rid] = ctx
                    continue
                sub = work / str(rid)
                sub.mkdir(parents=True, exist_ok=True)
                try:
                    local = self._collect(ctx, game, rec, sub)
                except SigilRefusal as e:
                    results[rid] = self._refusal(game, e)
                    continue
                except SavebridgeError as e:
                    results[rid] = result(rid, game["name"], "error", str(e))
                    continue
                if only_if_changed and not local.get("changed") and rec.last and not rec.doc.get("conflict"):
                    results[rid] = result(rid, game["name"], "up_to_date", "Nothing new on this device")
                    continue
                plans[rid] = {"game": game, "rec": rec, "ctx": ctx, "local": local, "dir": sub}
            if plans:
                self._negotiate_and_apply(plans, results, resolutions, trigger)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        for r in results.values():
            r["trigger"] = trigger
            r["notify"] = bool(self.settings.get("saves.notify"))
            self.emit("sync_result", r)
        return [results[r] for r in rom_ids if r in results]

    def _refusal(self, game: dict, e: SigilRefusal) -> dict:
        rid, name = game["rom_id"], game["name"]
        table = {
            "damaged": "A memory card file looks damaged" + (f": {e.problem}" if e.problem else ""),
            "ambiguous": "More than one card could hold this game's saves" + (f": {e.problem}" if e.problem else ""),
            "no_space": "The memory card is full" + (f" ({e.blocks_short} blocks short)" if e.blocks_short else ""),
            "not_found": "RomM's save holds nothing for this game",
            "conflict": "The save on this device changed since the last sync",
            "unsupported_format": "The memory card is in a format the helper cannot read",
            "io": "A memory card file could not be read or written",
        }
        status = "conflict" if e.name == "conflict" else "error"
        return result(rid, name, status, table.get(e.name, e.message), reason=e.name, problem=e.problem)

    # -- negotiating --------------------------------------------------------------------------------------------

    def _negotiate_and_apply(self, plans: dict[int, dict], results: dict[int, dict], resolutions: dict[int, str],
                             trigger: str) -> None:
        s = self.settings
        client = self.get_client()
        device_id = s.get("auth.device_id")
        slot = s.get("saves.slot")
        saves = [self._client_state(p["ctx"], p["game"], p["local"]) for p in plans.values() if p["local"].get("unit_path")]
        emulator_filter = sorted({e for p in plans.values() for e in p["ctx"].profile.compat})
        try:
            resp = client.negotiate(device_id, saves, rom_ids=list(plans), emulators=emulator_filter)
        except RommError as e:
            if e.kind == "not_found" and self.reregister:
                try:
                    device_id = self.reregister()
                    resp = client.negotiate(device_id, saves, rom_ids=list(plans), emulators=emulator_filter)
                except RommError as e2:
                    return self._fail_all(plans, results, e2)
            else:
                return self._fail_all(plans, results, e)
        session_id = resp.get("session_id")
        ops = resp.get("operations") or []
        done = failed = 0
        for rid, p in plans.items():
            game, rec = p["game"], p["rec"]
            mine = [o for o in ops if o.get("rom_id") == rid and (o.get("slot") in (slot, None))]
            op = self._pick(mine)
            try:
                res = self._apply(client, p, op, device_id, session_id, resolutions.get(rid), trigger)
            except RommError as e:
                if e.kind == "conflict":
                    res = self._make_conflict(client, p, "server_moved", e.detail or "RomM has a newer save", device_id, slot)
                else:
                    res = result(rid, game["name"], "offline" if e.kind == "network" else "error", describe(e))
            except SigilRefusal as e:
                if e.name == "conflict":
                    res = self._make_conflict(client, p, "both_changed", "The save on this device changed since the last sync",
                                              device_id, slot, op)
                else:
                    res = self._refusal(game, e)
            except (SavebridgeError, OSError) as e:
                res = result(rid, game["name"], "error", str(e))
            results[rid] = res
            if res["status"] in ("uploaded", "downloaded", "up_to_date", "no_saves"):
                done += 1
            elif res["status"] in ("error", "offline", "blocked"):
                failed += 1
            rec.save()
        if session_id is not None:
            try:
                client.complete_session(session_id, done, failed)
            except RommError as e:
                log.debug("could not close sync session %s: %s", session_id, e)

    @staticmethod
    def _pick(ops: list[dict]) -> dict | None:
        for action in ("conflict", "download", "upload", "no_op"):
            for o in ops:
                if o.get("action") == action:
                    return o
        return None

    def _fail_all(self, plans: dict[int, dict], results: dict[int, dict], e: RommError) -> None:
        status = "offline" if e.kind == "network" else ("auth" if e.kind == "auth" else "error")
        for rid, p in plans.items():
            results[rid] = result(rid, p["game"]["name"], status, describe(e))
            if status == "auth":
                self.emit("auth_required", {})

    # -- applying one operation -------------------------------------------------------------------------------

    def _apply(self, client: RommClient, p: dict, op: dict | None, device_id: str, session_id: int | None,
               choice: str | None, trigger: str) -> dict:
        game, rec, local = p["game"], p["rec"], p["local"]
        rid, name = game["rom_id"], game["name"]
        has_local = bool(local.get("unit_path"))
        action = (op or {}).get("action", "upload" if has_local else "no_op")
        server_hash = (op or {}).get("server_content_hash")
        has_server = bool((op or {}).get("save_id"))
        first_contact = rec.last is None

        # sigil noticed that the saves this device last restored were overwritten (a program wrote its own copy of the
        # card back over them). What is on the card now is older than RomM's, so it must not be uploaded: restore again.
        if local.get("restore_again") and rec.last and rec.last.get("action") == "download":
            again = (op or {}).get("save_id") if action == "download" else rec.last.get("server_save_id")
            if again:
                return self._download(client, p, op if action == "download" else None, device_id, session_id,
                                      force_local=True, save_id=again)

        # a standing conflict answered by the user
        pending = rec.doc.get("conflict")
        if choice and (pending or action == "conflict" or (first_contact and has_local and has_server)):
            return self._resolve(client, p, op, device_id, session_id, choice)

        # never replace a save on a guess: this device has no history of the game and both sides hold different saves
        reason = "first_contact" if first_contact and has_local and has_server else "both_changed"
        if first_contact and has_local and has_server and server_hash != local.get("content_hash") and action in ("upload", "download"):
            action = "conflict"
        # RomM keeps a sync record per saved version, so a device that has not seen the newest version is judged
        # by timestamps alone and told to download even when its own save changed. Hashes decide here: a save
        # that changed on this device since the last sync is never replaced without asking.
        elif (action == "download" and has_local and local.get("changed") and rec.last
              and server_hash and server_hash != local.get("content_hash")):
            action = "conflict"

        if action == "no_op":
            rec.doc["conflict"] = None
            if has_local and (rec.last is None or rec.last.get("local_hash") != local["content_hash"]):
                rec.doc["last"] = {"at": iso(), "action": "match", "local_hash": local["content_hash"],
                                   "server_hash": server_hash or local["content_hash"], "server_save_id": (op or {}).get("save_id")}
                rec.commit_state(Path(local["state_tmp"]) if local.get("state_tmp") else None)
            return result(rid, name, "up_to_date" if has_local or has_server else "no_saves",
                          "Already in sync" if has_local or has_server else "No saves yet, here or on RomM")
        if action == "upload":
            if not has_local:
                return result(rid, name, "no_saves", "No saves for this game on this device")
            return self._upload(client, p, device_id, session_id, force=False)
        if action == "download":
            return self._download(client, p, op, device_id, session_id, force_local=False)
        if action == "conflict":
            policy = self.settings.get("saves.conflict")
            if policy == "ask":
                return self._make_conflict(client, p, reason, (op or {}).get("reason", ""), device_id, self.settings.get("saves.slot"), op)
            pick = {"server": "server", "device": "device"}.get(policy)
            if policy == "newest":
                server_ts = _parse(op.get("server_updated_at")) if op else 0
                pick = "server" if server_ts > (local.get("mtime") or 0) else "device"
            return self._resolve(client, p, op, device_id, session_id, pick or "server")
        if action == "delete":
            return result(rid, name, "up_to_date", "RomM emptied this slot; the save on this device was left alone")
        return result(rid, name, "up_to_date", "Nothing to do")

    # -- upload / download / resolve ----------------------------------------------------------------------------

    def _upload(self, client: RommClient, p: dict, device_id: str, session_id: int | None, force: bool,
                slot: str | None = None, autocleanup: bool | None = None) -> dict:
        s = self.settings
        game, rec, local = p["game"], p["rec"], p["local"]
        data = Path(local["unit_path"]).read_bytes()
        use_slot = slot or s.get("saves.slot")
        auto = s.get("saves.autocleanup") if autocleanup is None else autocleanup
        saved = client.upload_save(
            game["rom_id"], data, local["artifact"], emulator=p["ctx"].profile.romm_emulator, slot=use_slot,
            device_id=device_id, content_hash=local["content_hash"], session_id=session_id, overwrite=force,
            autocleanup=auto, autocleanup_limit=s.get("saves.autocleanup_limit"))
        if use_slot == s.get("saves.slot"):
            rec.doc["last"] = {"at": iso(), "action": "upload", "local_hash": local["content_hash"],
                               "server_hash": saved.get("content_hash") or local["content_hash"],
                               "server_save_id": saved.get("id"), "identity_hash": local.get("identity_hash")}
            rec.doc["conflict"] = None
            rec.commit_state(Path(local["state_tmp"]) if local.get("state_tmp") else None)
        rec.note("upload", f"{len(data)} bytes to slot {use_slot}")
        return result(game["rom_id"], game["name"], "uploaded", "Uploaded this device's save to RomM",
                      save_id=saved.get("id"), slot=use_slot)

    def _download(self, client: RommClient, p: dict, op: dict | None, device_id: str, session_id: int | None,
                  force_local: bool, save_id: int | None = None) -> dict:
        s = self.settings
        game, rec, ctx = p["game"], p["rec"], p["ctx"]
        rid, name = game["rom_id"], game["name"]
        save_id = save_id or (op or {}).get("save_id")
        if not save_id:
            return result(rid, name, "error", "RomM named no save to download")
        if self.emulator_running(game):
            return result(rid, name, "busy", "The emulator started; the save is restored after it closes")
        data = client.download_save(save_id, device_id, session_id=session_id)
        expect = (op or {}).get("server_content_hash")
        if expect and hashlib.md5(data).hexdigest() != expect:
            return result(rid, name, "error", "The download did not match RomM's checksum; nothing was changed")
        unit = p["dir"] / "incoming.bin"
        unit.write_bytes(data)
        if s.get("saves.backups"):
            cards = [sl.path for sl in ctx.cfg.slots if sl.enabled]
            backup_cards(cards, rid, "before-restore", s.get("saves.backup_keep"))
        state_out = p["dir"] / "state.restore"
        got = self.bridge.restore(layout=ctx.profile.layout, root=ctx.root, unit=unit, rom=game.get("main_file"),
                                  identity=self.identity(game), platform_slug=ctx.profile.sigil_platform,
                                  content=game.get("main_file"), options=ctx.options,
                                  game_ids=[self.identity(game).get("title_id")] if self.identity(game).get("title_id") else None,
                                  state_in=rec.state_path, state_out=state_out, overwrite_local=force_local)
        rec.commit_state(state_out)
        # what a collect gives now is what the server should count as this device's copy
        post = self._collect(ctx, game, rec, p["dir"] / "post")
        rec.commit_state(Path(post["state_tmp"]) if post.get("state_tmp") else None)
        baseline = post.get("content_hash") or got.get("content_hash")
        try:
            client.confirm_download(save_id, device_id, baseline)
        except RommError as e:
            log.debug("could not confirm the download: %s", e)
        rec.doc["last"] = {"at": iso(), "action": "download", "local_hash": baseline,
                           "server_hash": got.get("content_hash"), "server_save_id": save_id,
                           "identity_hash": got.get("identity_hash")}
        rec.doc["conflict"] = None
        rec.note("download", f"{len(data)} bytes from save {save_id}")
        return result(rid, name, "downloaded", "Restored RomM's save onto this device", save_id=save_id)

    def _make_conflict(self, client: RommClient, p: dict, reason: str, detail: str, device_id: str, slot: str,
                       op: dict | None = None) -> dict:
        game, rec, local = p["game"], p["rec"], p["local"]
        server: dict = {"save_id": (op or {}).get("save_id"), "updated_at": (op or {}).get("server_updated_at"),
                        "hash": (op or {}).get("server_content_hash"), "device": ""}
        try:
            for sv in client.saves(game["rom_id"], device_id=device_id, slot=slot)[:1]:
                server.update({"save_id": sv.get("id"), "updated_at": sv.get("updated_at"), "hash": sv.get("content_hash")})
                for d in sv.get("device_syncs") or []:
                    if d.get("device_id") != device_id and d.get("device_name"):
                        server["device"] = d["device_name"]
                        break
        except RommError:
            pass
        conflict = {"rom_id": game["rom_id"], "name": game["name"], "reason": reason, "detail": detail,
                    "local": {"updated_at": ts_to_iso(local["mtime"]) if local.get("mtime") else None,
                              "hash": local.get("content_hash"), "has_saves": bool(local.get("unit_path"))},
                    "server": server, "at": iso()}
        rec.doc["conflict"] = conflict
        rec.note("conflict", reason)
        explain = ("This device has never synced this game, and RomM already has a different save. Choose which to keep."
                   if reason == "first_contact" else "The save changed here and on RomM. Choose which to keep.")
        return result(game["rom_id"], game["name"], "conflict", explain, conflict=conflict)

    def _resolve(self, client: RommClient, p: dict, op: dict | None, device_id: str, session_id: int | None,
                 choice: str) -> dict:
        game, rec, local = p["game"], p["rec"], p["local"]
        rid, name = game["rom_id"], game["name"]
        conflict = rec.doc.get("conflict") or {}
        save_id = (op or {}).get("save_id") or (conflict.get("server") or {}).get("save_id")
        if choice == "device":
            if not local.get("unit_path"):
                return result(rid, name, "no_saves", "There is no save on this device to keep")
            return self._upload(client, p, device_id, session_id, force=True)
        if choice == "server":
            if not save_id:
                return result(rid, name, "error", "RomM has no save to restore")
            return self._download(client, p, op, device_id, session_id, force_local=True, save_id=save_id)
        if choice == "keep_both":
            if local.get("unit_path"):
                tag = time.strftime("device-%Y%m%d-%H%M%S")
                self._upload(client, p, device_id, session_id, force=True, slot=tag, autocleanup=False)
            if not save_id:
                return result(rid, name, "uploaded", "Kept this device's save on RomM in its own slot")
            res = self._download(client, p, op, device_id, session_id, force_local=True, save_id=save_id)
            res["message"] = "Kept this device's save in its own slot on RomM, then restored RomM's save"
            return res
        return result(rid, name, "error", f"Unknown choice '{choice}'")

    # -- small public helpers ------------------------------------------------------------------------------------

    def status(self, rom_id: int) -> dict:
        game = self.manifest.get(rom_id)
        rec = Record(rom_id)
        out: dict = {"rom_id": rom_id, "supported": False, "enabled": rec.enabled, "last": rec.last,
                     "conflict": rec.doc.get("conflict"), "history": rec.doc.get("history", [])[-5:],
                     "backups": list_backups(rom_id)[:3]}
        if not game:
            return out
        got = self._emulator_for(game)
        if got:
            r, _ = got
            cfg = emulators.card_config(r.data_dir, (game.get("identity") or {}).get("title_id"))
            out.update({"supported": True, "emulator": r.profile.name, "data_dir": str(r.data_dir),
                        "data_dir_exists": r.data_dir_exists,
                        "slots": [{"slot": s.number, "enabled": s.enabled, "name": s.name, "kind": s.kind} for s in cfg.slots]})
        return out

    def set_enabled(self, rom_id: int, enabled: bool) -> None:
        rec = Record(rom_id)
        rec.doc["enabled"] = bool(enabled)
        rec.save()

    def forget(self, rom_id: int) -> None:
        Record(rom_id).reset()


def emulators_platform(game: dict) -> str | None:
    slug = (game.get("platform_slug") or "").lower()
    return "ps2" if emulators.group_for_platform(slug) == "ps2" else None


def _parse(value: Any) -> float:
    from .util import parse_iso
    dt = parse_iso(value) if isinstance(value, str) else None
    return dt.timestamp() if dt else 0.0
