# SPDX-License-Identifier: MIT
"""The background loop. Every couple of seconds it looks at three things:

  * the emulator: a start is noted, an exit pushes that game's save (or every changed save, when the emulator
    was started from its own menu and the game is unknown) and reports the play time to RomM;
  * the clock: at startup, every N minutes and after the handheld wakes it pulls newer saves, while no
    emulator runs, so the card is current before the next launch;
  * RomM's install queue: games sent to this device from the web UI are downloaded.
"""
from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

from . import emulators, log, paths
from .downloads import DONE, ERROR
from .romm import RommError
from .util import iso, read_json, ts_to_iso, write_json

if TYPE_CHECKING:
    from .service import Service

TICK = 2.0
STARTUP_DELAY = 20.0
WAKE_GAP = 30.0          # a pause this long between ticks means the device slept
EXIT_SETTLE = 3.0        # after the emulator is gone, let the card finish landing on disk
INSTALL_POLL = 60.0
MIN_PLAY_SECONDS = 30
UNKNOWN_REPORT_AFTER = 20.0     # an emulator that has run this long with no game recognised is described in the log, once
SYSTEM_PATHS = ("/usr/", "/lib", "/etc/", "/opt/", "/var/lib/", "/var/cache/", "/var/tmp/", "/tmp/.mount_", "/run/user/",
                "/dev/", "/proc/", "/sys/")


class Watcher:
    def __init__(self, svc: "Service") -> None:
        self.svc = svc
        self._thread: threading.Thread | None = None
        self._wake = threading.Event()
        self._stop = threading.Event()
        self.session: dict | None = None
        self._held: int | None = None        # the one game whose disc the emulator held open on the last look
        self._next_pull: float | None = None
        self._next_install_poll = 0.0
        self._last_wall = time.time()
        self._workers: list[threading.Thread] = []
        self._tracked_installs: dict[int, str] = {}      # rom id -> RomM's request id
        self._playtime = paths.state_dir() / "playtime.json"

    # -- lifecycle ---------------------------------------------------------------------------------------

    def start(self) -> None:
        if self._thread:
            return
        s = self.svc.settings
        self._next_pull = time.monotonic() + STARTUP_DELAY if s.get("saves.pull_on_startup") else self._interval_from_now()
        self._thread = threading.Thread(target=self._loop, name="romm-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._thread = None

    def poke(self) -> None:
        """Settings or sign-in changed: look again now."""
        self._wake.set()

    def _interval_from_now(self) -> float | None:
        minutes = int(self.svc.settings.get("saves.pull_interval_min"))
        return time.monotonic() + minutes * 60 if minutes > 0 else None

    # -- the loop -------------------------------------------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - the loop must outlive any one bad tick
                log.exception("watcher tick failed")
            self._wake.wait(TICK)
            self._wake.clear()

    def tick(self) -> None:
        now_wall = time.time()
        slept = now_wall - self._last_wall > WAKE_GAP
        self._last_wall = now_wall
        self._watch_emulator()
        s = self.svc.settings
        if slept:
            self._spawn(self.svc.setup_cards, "wake")
        if not s.signed_in:
            return
        if slept and s.get("saves.pull_when_idle"):
            log.info("woke up; saves will be checked shortly")
            self._next_pull = time.monotonic() + 10
        self._maybe_pull()
        self._maybe_installs()
        self._flush_playtime()
        self._report_finished_installs()

    # -- the emulator -------------------------------------------------------------------------------------------

    def _identify(self, found: list) -> tuple[str | None, dict | None, str]:
        """The installed game the emulator is playing: (disc path, game, how it was found).

        Started with a disc on its command line (ES-DE, a launcher): that one. Started from the emulator's own menu: the
        disc image it keeps open. Scanning the game list opens discs for a moment, so a game counts only when it is the one
        game held open on two looks in a row."""
        rom = next((i.rom_path() for i in found if i.rom_path()), None)
        if rom:
            return rom, self.svc.manifest.by_path(rom), "command line"
        held = {g["rom_id"]: (path, g) for inst in found for path, g in self.svc.manifest.by_paths(inst.open_files())}
        if len(held) == 1:
            rid, (path, game) = next(iter(held.items()))
            if self._held == rid:
                return path, game, "open disc image"
            self._held = rid
        else:
            self._held = None
        return None, None, ""

    def _watch_emulator(self) -> None:
        found = emulators.find_instances(emulators.all_process_names(), self.svc.proc_root)
        if found and self.session is None:
            inst = found[0]
            rom, game, how = self._identify(found)
            self.session = {"started": time.time(), "rom_path": rom, "rom_id": game["rom_id"] if game else None,
                            "name": game["name"] if game else None, "pids": {i.pid for i in found}, "emulator": inst.comm,
                            "found_by": how if game else ""}
            log.info("emulator started: %s %s", inst.comm, game["name"] if game else (rom or "(no game on its command line yet)"))
            self.svc.emit("emulator_started", {"rom_id": self.session["rom_id"], "name": self.session["name"]})
        elif found and self.session is not None:
            self.session["pids"] |= {i.pid for i in found}
            if self.session["rom_id"] is None:
                rom, game, how = self._identify(found)
                if game:
                    self.session.update(rom_path=rom, rom_id=game["rom_id"], name=game["name"], found_by=how)
                    log.info("the emulator is playing %s (found by its %s)", game["name"], how)
                    self.svc.emit("emulator_started", {"rom_id": game["rom_id"], "name": game["name"]})
                elif not self.session.get("described") and time.time() - self.session["started"] > UNKNOWN_REPORT_AFTER:
                    self.session["described"] = True
                    held = [p for i in found for p in i.open_files() if not p.startswith(SYSTEM_PATHS)]
                    log.info("no installed game recognised after %ss. Command line: %s. Files held open: %s",
                             int(time.time() - self.session["started"]), " ".join(found[0].cmdline[1:]) or "-",
                             ", ".join(held[:8]) or "none")
        elif not found and self.session is not None:
            session, self.session = self.session, None
            self._held = None
            self._on_exit(session)

    def _on_exit(self, session: dict) -> None:
        seconds = int(time.time() - session["started"])
        log.info("emulator closed after %ss: %s", seconds, session.get("name") or "unknown game")
        self.svc.emit("emulator_stopped", {"rom_id": session["rom_id"], "name": session["name"], "seconds": seconds})
        self._spawn(self.svc.setup_cards, "exit")        # a first start leaves empty file cards behind; make them folder cards now
        s = self.svc.settings
        if session["rom_id"] and s.get("saves.playtime") and seconds >= MIN_PLAY_SECONDS:
            self._queue_playtime(session, seconds)
        if s.signed_in and s.get("saves.enabled") and s.get("saves.push_on_exit"):
            if session["rom_id"] and session.get("found_by") != "open disc image":
                ids = [session["rom_id"]]
            else:
                # the game is unknown, or was told from its open disc: the emulator's own menu can switch games without closing,
                # so every installed game is checked, this one first. Only the ones whose save changed go to RomM.
                ids = self._supported_ids()
                if session["rom_id"] in ids:
                    ids.remove(session["rom_id"])
                    ids.insert(0, session["rom_id"])
            self._spawn(self._push_after_exit, ids)
        self._wake.set()

    def _push_after_exit(self, ids: list[int]) -> None:
        time.sleep(EXIT_SETTLE)
        if not ids:
            return
        self.svc.saves.sync(ids, trigger="exit", only_if_changed=True)
        self._maybe_toast_conflicts()

    # -- pulling --------------------------------------------------------------------------------------------------

    def _supported_ids(self) -> list[int]:
        return [g["rom_id"] for g in self.svc.manifest.all() if emulators.group_for_platform(g.get("platform_slug", ""))]

    def _maybe_pull(self) -> None:
        s = self.svc.settings
        if self._next_pull is None or time.monotonic() < self._next_pull:
            return
        if self.session is not None or not s.get("saves.enabled") or not s.get("saves.pull_when_idle"):
            self._next_pull = self._interval_from_now() if self.session is None else time.monotonic() + 60
            return
        self._next_pull = self._interval_from_now()
        ids = self._supported_ids()
        if ids:
            self._spawn(self._pull, ids)

    def _pull(self, ids: list[int]) -> None:
        self.svc.saves.sync(ids, trigger="auto")
        self._maybe_toast_conflicts()

    def _maybe_toast_conflicts(self) -> None:
        if not self.svc.settings.get("saves.notify"):
            return
        for c in self.svc.conflicts():
            self.svc.emit("conflict", c)

    def _spawn(self, fn, *args) -> None:
        self._workers = [w for w in self._workers if w.is_alive()]
        t = threading.Thread(target=self._guard, args=(fn, *args), name="romm-sync", daemon=True)
        self._workers.append(t)
        t.start()

    @staticmethod
    def _guard(fn, *args) -> None:
        try:
            fn(*args)
        except Exception:  # noqa: BLE001
            log.exception("background sync failed")

    # -- play time ---------------------------------------------------------------------------------------------------

    def _queue_playtime(self, session: dict, seconds: int) -> None:
        q = read_json(self._playtime, [])
        q = q if isinstance(q, list) else []
        end = time.time()
        q.append({"rom_id": session["rom_id"], "start_time": ts_to_iso(session["started"]).split(".")[0] + "Z",
                  "end_time": ts_to_iso(end).split(".")[0] + "Z", "duration_ms": seconds * 1000,
                  "save_slot": self.svc.settings.get("saves.slot")})
        write_json(self._playtime, q[-200:])

    def _flush_playtime(self) -> None:
        q = read_json(self._playtime, [])
        if not isinstance(q, list) or not q:
            return
        s = self.svc.settings
        if not s.get("auth.device_id") or self.session is not None:
            return
        try:
            self.svc.client().play_sessions(s.get("auth.device_id"), q[:100])
            write_json(self._playtime, q[100:])
        except RommError as e:
            if e.kind in ("client", "forbidden", "not_found"):   # the server will never take these
                write_json(self._playtime, q[100:])
            log.debug("play time not sent yet: %s", e)

    # -- install requests from RomM -------------------------------------------------------------------------------------

    def _maybe_installs(self) -> None:
        s = self.svc.settings
        if not s.get("library.remote_installs") or time.monotonic() < self._next_install_poll:
            return
        self._next_install_poll = time.monotonic() + INSTALL_POLL
        try:
            client = self.svc.client()
            requests = client.claim_installs(s.get("auth.device_id"))
        except RommError as e:
            log.debug("install requests not fetched: %s", e)
            return
        for req in requests:
            rid, req_id = int(req["rom_id"]), req["id"]
            if rid in self._tracked_installs or self.svc.downloads.get(rid) and self.svc.downloads.get(rid)["state"] in (
                    "queued", "running", "paused", "verifying"):
                continue
            if self.svc.manifest.get(rid):
                self._report(req_id, "already_installed")
                continue
            try:
                rom = client.rom(rid)
                self.svc.downloads.enqueue(rom, file_ids=req.get("file_ids") or None)
                self._tracked_installs[rid] = req_id
            except (RommError, ValueError) as e:
                self._report(req_id, "failed", str(e)[:400])

    def _report(self, req_id: str, status: str, reason: str | None = None) -> None:
        try:
            self.svc.client().report_install(self.svc.settings.get("auth.device_id"), req_id, status, reason)
        except RommError as e:
            log.debug("could not report install %s: %s", req_id, e)

    def _report_finished_installs(self) -> None:
        for rid, req_id in list(self._tracked_installs.items()):
            job = self.svc.downloads.get(rid)
            if job and job["state"] == DONE:
                self._report(req_id, "done")
            elif job and job["state"] == ERROR:
                self._report(req_id, "failed", (job.get("error") or "download failed")[:400])
            elif job and job["state"] not in ("cancelled",):
                continue
            del self._tracked_installs[rid]
