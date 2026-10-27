# SPDX-License-Identifier: MIT
"""Save states: the quick-save files an emulator writes beside its memory cards.

RomM keeps them apart from saves. A state has no slot, no version history and no device record: it is one row per
(game, file name), and uploading a name again replaces it. So the plugin keeps the file names the emulators use
(`SLUS-21269 (28703748).01.p2s`, `GZLE01.s01`, `Game.state1`) and files them under the emulator's own folder on RomM
(`armsx2`, `pcsx2`, `dolphin`, `retroarch`), which is also what keeps a state from one emulator out of another's way.

What is synced, per file name, with only RomM's timestamps and the plugin's own record of the last sync to go on:

  * a state that exists here and not on RomM is uploaded; one that changed here since the last sync is uploaded;
  * a state that exists on RomM and not here is downloaded, but not again once it was taken and then deleted here;
  * a state that RomM has newer than the last sync is downloaded, the old file kept aside first;
  * a state that changed on both sides is left alone on both, and said so.

Nothing is ever deleted on either side. A state only means something to the emulator build that made it, which is why
a state is replaced only by one for the same game under the same name.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import emulators, libretro, log, paths
from .romm import RommClient, RommError
from .util import parse_iso, read_json, write_json

MAX_BYTES = 256 * 1024 * 1024
CLOCK_SLACK = 2.0          # seconds: a file's date here against RomM's own clock, when the two meet for the first time


@dataclass
class Plan:
    tag: str                              # the emulator folder on RomM
    pattern: re.Pattern
    scan_dirs: list[Path]
    write_dir: Path | None
    why_not: str = ""                     # set when states cannot be placed (and so not downloaded)
    found: dict[str, Path] = field(default_factory=dict)

    def local(self) -> dict[str, Path]:
        """This game's state files by name; the newest when a name is in two folders."""
        out: dict[str, Path] = {}
        for d in self.scan_dirs:
            try:
                names = sorted(os.listdir(d))
            except OSError:
                continue
            for n in names:
                p = d / n
                try:
                    if self.pattern.match(n) and p.is_file() and not p.is_symlink() and 0 < p.stat().st_size <= MAX_BYTES:
                        if n not in out or p.stat().st_mtime > out[n].stat().st_mtime:
                            out[n] = p
                except OSError:
                    continue
        return out


def _subdirs(d: Path) -> list[Path]:
    try:
        return sorted(p for p in d.iterdir() if p.is_dir() and not p.is_symlink() and not p.name.startswith("."))
    except OSError:
        return []


def plan_for(settings, game: dict, ident: dict) -> Plan | None:
    """Where this game's states are, what their names look like, and where RomM files them. None for a game with no
    emulator here, or none that is known."""
    group = emulators.group_for_platform(game.get("platform_slug", ""))
    if group == "ps2":
        r = emulators.resolve_ps2(settings.get("emulators.ps2.emulator"), settings.get("emulators.ps2.data_dir"),
                                  settings.get("emulators.ps2.executable"))
        serial = (ident or {}).get("title_id") or game.get("title_id") or ""
        if not serial:
            return None
        raw = emulators.read_ini(r.ini).get("Folders", {}).get("Savestates", "sstates") or "sstates"
        d = Path(os.path.realpath(raw if os.path.isabs(raw) else r.data_dir / raw))
        return Plan(r.profile.id, re.compile(rf"^{re.escape(serial)} \([0-9A-Fa-f]+\)\.\d+\.p2s$", re.I), [d], d)
    if group == "dolphin":
        d = emulators.resolve_dolphin(settings.get("emulators.dolphin.data_dir"), settings.get("emulators.dolphin.executable"))
        gid = (ident or {}).get("save_id") or ""
        if not gid:
            return None
        sd = d.data_dir / "StateSaves"
        return Plan("dolphin", re.compile(rf"^{re.escape(gid)}\w\w\.s\d\d$", re.I), [sd], sd)
    if group == "retroarch":
        main = game.get("main_file") or ""
        if not main:
            return None
        r = libretro.resolve(settings.get("emulators.retroarch.data_dir"), settings.get("emulators.retroarch.save_dir"),
                             settings.get("emulators.retroarch.executable"))
        c = r.config
        base = c.state_dir if c.state_dir is not None else Path(main).parent
        sorted_ = c.state_dir is not None and (c.state_sort_by_core or c.state_sort_by_content)
        dirs = [base]
        if sorted_:
            for sub in _subdirs(base):
                dirs.append(sub)
                if c.state_sort_by_core and c.state_sort_by_content:
                    dirs += _subdirs(sub)
        plan = Plan("retroarch", re.compile(rf"^{re.escape(Path(main).stem)}\.state\d*(\.auto)?$", re.I), dirs, base)
        if sorted_:
            holding = [d for d in dirs if any(plan.pattern.match(n) for n in _listdir(d))]
            if holding:
                plan.write_dir = holding[0]
            elif c.state_sort_by_content and not c.state_sort_by_core:
                plan.write_dir = base / Path(main).parent.name
            else:
                plan.write_dir, plan.why_not = None, "RetroArch keeps states in a folder per core, and this game has none yet"
        return plan
    return None


def _listdir(d: Path) -> list[str]:
    try:
        return os.listdir(d)
    except OSError:
        return []


def _md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _ts(value) -> float:
    dt = parse_iso(value) if isinstance(value, str) else None
    return dt.timestamp() if dt else 0.0


class StateSync:
    def __init__(self, settings, get_client: Callable[[], RommClient], identity: Callable[[dict], dict]) -> None:
        self.settings = settings
        self.get_client = get_client
        self.identity = identity

    # -- the record of the last sync ---------------------------------------------------------------------------

    @staticmethod
    def _record_path(rom_id: int) -> Path:
        return paths.state_dir() / "states" / f"{rom_id}.json"

    def _load(self, rom_id: int) -> dict:
        doc = read_json(self._record_path(rom_id), {})
        return doc if isinstance(doc, dict) and isinstance(doc.get("files"), dict) else {"files": {}}

    def _save(self, rom_id: int, doc: dict) -> None:
        write_json(self._record_path(rom_id), doc)

    def _hash(self, path: Path, rec: dict | None) -> str:
        st = path.stat()
        if rec and rec.get("size") == st.st_size and rec.get("mtime_ns") == st.st_mtime_ns and rec.get("hash"):
            return rec["hash"]
        return _md5(path)

    # -- one game ------------------------------------------------------------------------------------------------

    def sync(self, game: dict, only_if_changed: bool = False) -> dict | None:
        """Brings this game's states in line with RomM. Returns {uploaded, downloaded, left, errors} (names), or None when
        there is nothing to look at. A failing RomM call raises RommError for the caller to report."""
        if not self.settings.get("states.enabled"):
            return None
        rid = game["rom_id"]
        plan = plan_for(self.settings, game, self.identity(game) if emulators.group_for_platform(game.get("platform_slug", "")) in ("ps2", "dolphin") else {})
        if plan is None:
            return None
        local = plan.local()
        if only_if_changed:          # after the emulator closes: RomM is asked only when a state here is new or changed
            known = self._load(rid)["files"]
            if all(known.get(n) and self._hash(p, known[n]) == known[n].get("hash") for n, p in local.items()):
                return None
        client = self.get_client()
        server = {s["file_name"]: s for s in client.states(rid) if plan.pattern.match(s.get("file_name") or "") and s.get("emulator") == plan.tag}
        if not local and not server:
            return None
        doc = self._load(rid)
        files: dict = doc["files"]
        report: dict = {"uploaded": [], "downloaded": [], "left": [], "errors": []}
        for name in sorted(set(local) | set(server)):
            lp, sv, rec = local.get(name), server.get(name), files.get(name)
            try:
                self._one(client, rid, plan, name, lp, sv, rec, files, report)
            except RommError:
                raise
            except OSError as e:
                report["errors"].append(f"{name}: {e}")
                log.warn("state %s of %s: %s", name, game.get("name"), e)
        self._save(rid, doc)
        return report

    def _one(self, client: RommClient, rid: int, plan: Plan, name: str, lp: Path | None, sv: dict | None, rec: dict | None,
             files: dict, report: dict) -> None:
        sv_new = bool(sv) and (not rec or _ts(sv.get("updated_at")) > _ts(rec.get("server_updated_at")))
        lh = self._hash(lp, rec) if lp else ""
        changed = bool(lp) and (not rec or rec.get("hash") != lh)
        if lp and (not sv):
            if changed:
                self._upload(client, rid, plan, name, lp, lh, files, report)
            return
        if lp and sv:
            if not rec:                         # both exist and this device has never synced the name: the newer one wins
                if lp.stat().st_mtime > _ts(sv.get("updated_at")) + CLOCK_SLACK:
                    self._upload(client, rid, plan, name, lp, lh, files, report)
                elif plan.write_dir is not None:
                    self._download(client, rid, plan, name, sv, lp, files, report)
                return
            if changed and sv_new:
                report["left"].append(name)
                return
            if changed:
                self._upload(client, rid, plan, name, lp, lh, files, report)
            elif sv_new and plan.write_dir is not None:
                self._download(client, rid, plan, name, sv, lp, files, report)
            return
        if sv and not lp and (not rec or sv_new):          # RomM has it, this device does not (and did not delete it)
            if plan.write_dir is None:
                report["errors"].append(f"{name}: {plan.why_not or 'nowhere to put it'}")
                return
            self._download(client, rid, plan, name, sv, None, files, report)

    def _upload(self, client: RommClient, rid: int, plan: Plan, name: str, path: Path, digest: str, files: dict, report: dict) -> None:
        st = path.stat()
        saved = client.upload_state(rid, path.read_bytes(), name, plan.tag)
        files[name] = {"hash": digest, "size": st.st_size, "mtime_ns": st.st_mtime_ns,
                       "server_id": saved.get("id"), "server_updated_at": saved.get("updated_at"), "action": "upload"}
        report["uploaded"].append(name)
        log.info("save state %s uploaded (%d bytes)", name, st.st_size)

    def _download(self, client: RommClient, rid: int, plan: Plan, name: str, sv: dict, lp: Path | None, files: dict, report: dict) -> None:
        data = client.download_state(sv["id"])
        if not data or len(data) > MAX_BYTES:
            report["errors"].append(f"{name}: RomM's state is empty or too large")
            return
        target = (lp.parent if lp else plan.write_dir)
        assert target is not None
        if lp and self.settings.get("saves.backups"):
            keep = paths.backups_dir() / f"game-{rid}" / "states"
            keep.mkdir(parents=True, exist_ok=True)
            shutil.copy2(lp, keep / name)
        target.mkdir(parents=True, exist_ok=True)
        tmp = target / f".{name}.romm-armada"
        try:
            tmp.write_bytes(data)
            os.chmod(tmp, 0o644)
            os.replace(tmp, target / name)
        finally:
            tmp.unlink(missing_ok=True)
        st = (target / name).stat()
        files[name] = {"hash": hashlib.md5(data).hexdigest(), "size": st.st_size, "mtime_ns": st.st_mtime_ns,
                       "server_id": sv.get("id"), "server_updated_at": sv.get("updated_at"), "action": "download"}
        report["downloaded"].append(name)
        log.info("save state %s restored from RomM (%d bytes)", name, len(data))
