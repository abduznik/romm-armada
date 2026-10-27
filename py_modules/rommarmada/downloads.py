# SPDX-License-Identifier: MIT
"""Downloading games into the library.

One job per game. A job downloads the game's files one after another, each into `<name>.part` next to its
final place, resuming with an HTTP Range request where a part file exists, and checking the CRC32 RomM holds
for the file as it streams. A finished file is renamed into place; a finished job is added to the manifest.

The queue is saved, so a restart picks up where it stopped (or waits, if "Resume on start" is off).
"""
from __future__ import annotations

import errno
import http.client
import os
import shutil
import threading
import time
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import log, paths
from .config import Settings
from .http import HttpError, NetworkError
from .library import Layout, Manifest, choose_main_file, make_entry, pick_files
from .romm import RommClient, RommError, describe
from .util import Throttle, free_bytes, human_bytes, is_within, iso, now_utc, read_json, write_json

CHUNK = 256 * 1024
RETRY_DELAYS = (2, 5, 10, 20, 30)
EMIT_EVERY = 0.4

QUEUED, RUNNING, PAUSED, VERIFYING, DONE, ERROR, CANCELLED = (
    "queued", "running", "paused", "verifying", "done", "error", "cancelled")
ACTIVE = (QUEUED, RUNNING, VERIFYING)


class Stop(Exception):
    """Raised inside a worker to leave the current job (paused or cancelled)."""


@dataclass
class FileTask:
    id: int
    name: str
    size: int
    dest: str
    crc: str = ""
    done: int = 0

    @property
    def part(self) -> str:
        return self.dest + ".part"


@dataclass
class Job:
    rom: dict
    files: list[FileTask]
    state: str = QUEUED
    error: str = ""
    error_kind: str = ""
    created: str = field(default_factory=iso)
    finished: str = ""
    speed: float = 0.0
    _stop: str = ""                # "", "pause" or "cancel"; set from other threads
    _since: float = 0.0
    _since_bytes: int = 0

    @property
    def rom_id(self) -> int:
        return int(self.rom["id"])

    @property
    def total(self) -> int:
        return sum(f.size for f in self.files)

    @property
    def done(self) -> int:
        return sum(min(f.done, f.size) if f.size else f.done for f in self.files)

    def view(self) -> dict:
        total, done = self.total, self.done
        eta = int((total - done) / self.speed) if self.speed > 1 and total > done and self.state == RUNNING else 0
        return {"rom_id": self.rom_id, "name": self.rom.get("name") or self.rom.get("fs_name"),
                "platform": self.rom.get("platform_display_name") or self.rom.get("platform_slug"),
                "platform_slug": self.rom.get("platform_slug"), "state": self.state, "error": self.error,
                "error_kind": self.error_kind, "total": total, "done": done, "speed": int(self.speed), "eta": eta,
                "files": len(self.files), "created": self.created, "finished": self.finished}

    def persist(self) -> dict:
        return {"rom": self.rom, "state": self.state, "error": self.error, "error_kind": self.error_kind,
                "created": self.created, "finished": self.finished,
                "files": [{"id": f.id, "name": f.name, "size": f.size, "dest": f.dest, "crc": f.crc, "done": f.done}
                          for f in self.files]}


def crc_matches(expected: str, actual: int) -> bool:
    expected = (expected or "").strip().lower().lstrip("0x") or "0"
    try:
        return int(expected, 16) == actual
    except ValueError:
        return True  # a hash we cannot read is not a reason to throw the file away


def zip_member_crcs(path: str) -> set[int] | None:
    """The CRC32 of each file inside a zip, from a zip that reads back whole. RomM lists the CRC of one file inside a zipped
    game (the first), not of the zip, and some zips hold a dozen versions of the game. None when the zip is damaged."""
    try:
        with zipfile.ZipFile(path) as zf:
            if zf.testzip() is not None:
                return None
            return {i.CRC for i in zf.infolist() if not i.is_dir()}
    except (zipfile.BadZipFile, OSError, RuntimeError, NotImplementedError):
        return None


def crc_of_file(path: str) -> int:
    crc = 0
    with open(path, "rb") as f:
        while True:
            b = f.read(1 << 20)
            if not b:
                break
            crc = zlib.crc32(b, crc)
    return crc


class DownloadManager:
    def __init__(self, settings: Settings, manifest: Manifest, get_client: Callable[[], RommClient],
                 emit: Callable[[str, dict], None] | None = None,
                 on_installed: Callable[[dict], None] | None = None) -> None:
        self.settings = settings
        self.manifest = manifest
        self.get_client = get_client
        self.emit = emit or (lambda e, p: None)
        self.on_installed = on_installed
        self.jobs: dict[int, Job] = {}
        self.order: list[int] = []
        self.throttle = Throttle()
        self._cv = threading.Condition(threading.RLock())
        self._threads: list[threading.Thread] = []
        self._running = False
        self._store = paths.state_dir() / "downloads.json"
        self._last_emit: dict[int, float] = {}

    # -- lifecycle ---------------------------------------------------------------------------------------

    def start(self) -> None:
        with self._cv:
            if self._running:
                return
            self._running = True
        self._load()
        for i in range(3):  # up to the largest "concurrent downloads" setting; extra workers idle
            t = threading.Thread(target=self._worker, args=(i,), name=f"romm-dl-{i}", daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        with self._cv:
            self._running = False
            for j in self.jobs.values():
                if j.state in (RUNNING, VERIFYING):
                    j._stop = "pause"
            self._cv.notify_all()
        for t in self._threads:
            t.join(timeout=5)
        self._threads.clear()
        self._save()

    def _load(self) -> None:
        doc = read_json(self._store, {})
        resume = self.settings.get("library.resume_on_start")
        with self._cv:
            for item in (doc.get("jobs") if isinstance(doc, dict) else None) or []:
                try:
                    files = [FileTask(int(f["id"]), f["name"], int(f["size"]), f["dest"], f.get("crc", ""), int(f.get("done", 0)))
                             for f in item["files"]]
                    job = Job(item["rom"], files, item.get("state", QUEUED), item.get("error", ""),
                              item.get("error_kind", ""), item.get("created", iso()), item.get("finished", ""))
                except (KeyError, TypeError, ValueError):
                    continue
                if job.state in (RUNNING, VERIFYING, QUEUED):
                    job.state = QUEUED if resume else PAUSED
                self.jobs[job.rom_id] = job
                self.order.append(job.rom_id)
            self._cv.notify_all()

    def _save(self) -> None:
        with self._cv:
            doc = {"version": 1, "jobs": [self.jobs[r].persist() for r in self.order if r in self.jobs]}
        try:
            write_json(self._store, doc)
        except OSError as e:
            log.warn("could not save the download queue: %s", e)

    # -- the queue -----------------------------------------------------------------------------------------

    def layout(self) -> Layout:
        s = self.settings
        return Layout(s.get("library.root"), s.get("library.folder_style"), s.get("library.platform_dirs"))

    def enqueue(self, rom: dict, file_ids: list[int] | None = None) -> dict:
        """Queues a game. `rom` is RomM's detail record (it must carry `files`); `file_ids` narrows it to some of them."""
        files = pick_files(rom, self.settings.get("library.include_extras"))
        if file_ids:
            wanted = {int(i) for i in file_ids}
            files = [f for f in files if int(f["id"]) in wanted]
        if not files:
            raise ValueError("RomM lists no files for this game")
        layout = self.layout()
        tasks = [FileTask(int(f["id"]), f.get("file_name") or f"file-{f['id']}", int(f.get("file_size_bytes") or 0),
                          str(layout.destination(rom, f)), (f.get("crc_hash") or "")) for f in files]
        keep = {k: rom.get(k) for k in (
            "id", "name", "fs_name", "fs_name_no_ext", "platform_id", "platform_slug", "platform_fs_slug",
            "platform_display_name", "full_path", "has_multiple_files", "has_nested_single_file", "title_id",
            "fs_size_bytes", "path_cover_small", "regions", "languages")}
        keep["files"] = [{k: f.get(k) for k in ("id", "file_name", "file_path", "full_path", "file_size_bytes",
                                                  "crc_hash", "md5_hash", "sha1_hash", "category")} for f in files]
        rid = int(rom["id"])
        with self._cv:
            old = self.jobs.get(rid)
            if old and old.state in ACTIVE + (PAUSED,):
                return old.view()
            job = Job(keep, tasks)
            self.jobs[rid] = job
            if rid in self.order:
                self.order.remove(rid)
            self.order.append(rid)
            self._cv.notify_all()
        self._save()
        self._emit(job, force=True)
        return job.view()

    def pause(self, rom_id: int) -> None:
        with self._cv:
            j = self.jobs.get(rom_id)
            if not j:
                return
            if j.state == QUEUED:
                j.state = PAUSED
            elif j.state in (RUNNING, VERIFYING):
                j._stop = "pause"
        self._save()
        if j:
            self._emit(j, force=True)

    def resume(self, rom_id: int) -> None:
        with self._cv:
            j = self.jobs.get(rom_id)
            if j and j.state in (PAUSED, ERROR, CANCELLED):
                j.state, j.error, j.error_kind, j._stop = QUEUED, "", "", ""
                self._cv.notify_all()
        self._save()
        if j:
            self._emit(j, force=True)

    def cancel(self, rom_id: int) -> None:
        with self._cv:
            j = self.jobs.get(rom_id)
            if not j:
                return
            if j.state in (RUNNING, VERIFYING):
                j._stop = "cancel"
            else:
                self._discard_parts(j)
                j.state = CANCELLED
        self._save()
        self._emit(j, force=True)

    def clear(self, rom_id: int | None = None) -> None:
        """Forgets finished, failed and cancelled jobs (one, or all)."""
        with self._cv:
            for rid in list(self.order):
                j = self.jobs.get(rid)
                if j and j.state in (DONE, ERROR, CANCELLED) and (rom_id is None or rid == rom_id):
                    del self.jobs[rid]
                    self.order.remove(rid)
        self._save()

    def status(self) -> list[dict]:
        with self._cv:
            return [self.jobs[r].view() for r in self.order if r in self.jobs]

    def get(self, rom_id: int) -> dict | None:
        with self._cv:
            j = self.jobs.get(rom_id)
            return j.view() if j else None

    def _emit(self, job: Job, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_emit.get(job.rom_id, 0) < EMIT_EVERY:
            return
        self._last_emit[job.rom_id] = now
        self.emit("download_progress", job.view())

    # -- workers --------------------------------------------------------------------------------------------

    def _next_job(self, index: int) -> Job | None:
        with self._cv:
            while self._running:
                allowed = self.settings.get("library.max_concurrent")
                busy = sum(1 for j in self.jobs.values() if j.state in (RUNNING, VERIFYING))
                if index < allowed and busy < allowed:
                    for rid in self.order:
                        j = self.jobs.get(rid)
                        if j and j.state == QUEUED:
                            j.state = RUNNING
                            j._stop = ""
                            j._since, j._since_bytes = time.monotonic(), j.done
                            return j
                self._cv.wait(timeout=2.0)
        return None

    def _worker(self, index: int) -> None:
        while True:
            job = self._next_job(index)
            if job is None:
                return
            self._emit(job, force=True)
            try:
                self._run(job)
                job.state = DONE
                job.finished = iso()
                job.speed = 0
            except Stop:
                if job._stop == "cancel":
                    self._discard_parts(job)
                    job.state = CANCELLED
                else:
                    job.state = PAUSED
                job.speed = 0
            except Exception as e:  # noqa: BLE001 - a job must never take the worker down
                self._fail(job, e)
            job._stop = ""
            self._save()
            self._emit(job, force=True)
            if job.state == DONE and self.on_installed:
                try:
                    self.on_installed(self.manifest.get(job.rom_id) or {})
                except Exception:  # noqa: BLE001
                    log.exception("after-install hook failed")
            with self._cv:
                self._cv.notify_all()

    def _fail(self, job: Job, e: Exception) -> None:
        job.speed = 0
        job.state = ERROR
        job.finished = iso()
        if isinstance(e, RommError):
            job.error, job.error_kind = describe(e), e.kind
        elif isinstance(e, OSError) and e.errno == errno.ENOSPC:
            job.error, job.error_kind = str(e.strerror or "The drive is full"), "disk"
        elif isinstance(e, OSError):
            job.error, job.error_kind = f"Could not write the file: {e.strerror or e}", "disk"
        else:
            job.error, job.error_kind = str(e) or e.__class__.__name__, "error"
            log.exception("download of %s failed", job.rom.get("name"))
        log.warn("download of %s failed: %s", job.rom.get("name"), job.error)

    # -- one job ------------------------------------------------------------------------------------------------

    def _check_stop(self, job: Job) -> None:
        if job._stop or not self._running:
            raise Stop()

    def _run(self, job: Job) -> None:
        s = self.settings
        layout = self.layout()
        root = Path(s.get("library.root"))
        for f in job.files:
            if not is_within(root, f.dest):
                raise ValueError(f"{f.name} would land outside the library folder")
        self.throttle.rate = int(s.get("library.speed_limit_kbs")) * 1024
        # space: what is still to download plus the margin the user wants kept free
        need = sum(max(0, f.size - (os.path.getsize(f.part) if os.path.exists(f.part) else 0))
                   for f in job.files if not self._complete(f))
        margin = int(s.get("library.min_free_mb")) * 1024 * 1024
        have = free_bytes(root)
        if need + margin > have:
            raise OSError(errno.ENOSPC, f"Needs {human_bytes(need)} (plus {human_bytes(margin)} kept free) but only "
                                         f"{human_bytes(have)} is free")
        client = self.get_client()
        for f in job.files:
            self._check_stop(job)
            if self._complete(f):
                f.done = f.size
                continue
            self._download_file(client, job, f)
        job.state = VERIFYING
        self._emit(job, force=True)
        self._finish(job, layout)

    @staticmethod
    def _complete(f: FileTask) -> bool:
        try:
            return os.path.getsize(f.dest) == f.size and f.size > 0
        except OSError:
            return False

    def _download_file(self, client: RommClient, job: Job, f: FileTask) -> None:
        s = self.settings
        verify = s.get("library.verify")
        os.makedirs(os.path.dirname(f.dest), exist_ok=True)
        attempt = 0
        while True:
            self._check_stop(job)
            try:
                self._stream(client, job, f, verify)
                return
            except (NetworkError, HttpError) as e:
                err = RommError("network", e.message, net_kind=e.kind) if isinstance(e, NetworkError) else None
                if isinstance(e, HttpError):
                    if e.status in (401, 403, 404):
                        raise RommError({401: "auth", 403: "forbidden", 404: "not_found"}[e.status], e.detail or f"HTTP {e.status}",
                                        e.status, e.detail) from None
                    if e.status == 416:  # the part file is longer than the file: start over
                        self._drop_part(f)
                        continue
                    err = RommError("server" if e.status >= 500 else "client", e.detail or f"HTTP {e.status}", e.status)
                if attempt >= len(RETRY_DELAYS):
                    raise err from None
                delay = RETRY_DELAYS[attempt]
                attempt += 1
                log.info("download of %s interrupted (%s), retrying in %ss", f.name, err, delay)
                for _ in range(int(delay * 5)):
                    self._check_stop(job)
                    time.sleep(0.2)

    @staticmethod
    def _drop_part(f: FileTask) -> None:
        try:
            os.unlink(f.part)
        except FileNotFoundError:
            pass
        f.done = 0

    def _stream(self, client: RommClient, job: Job, f: FileTask, verify: str) -> None:
        start = os.path.getsize(f.part) if os.path.exists(f.part) else 0
        if f.size and start > f.size:
            self._drop_part(f)
            start = 0
        headers = {"Range": f"bytes={start}-"} if start else {}
        resp = client.http.open("GET", client.file_url_path(f.id, f.name), headers=headers, timeout=60)
        try:
            status = getattr(resp, "status", 200)
            crc = 0
            if status == 206 and start:
                mode = "ab"
                if verify == "crc" and f.crc:
                    crc = crc_of_file(f.part)
            else:
                mode = "wb"  # the server sent the whole file (or we asked for all of it)
                start = 0
            f.done = start
            length = resp.headers.get("Content-Length")
            if not f.size and length and length.isdigit():
                f.size = start + int(length)
            with open(f.part, mode) as out:
                while True:
                    self._check_stop(job)
                    try:
                        chunk = resp.read(CHUNK)
                    except (http.client.HTTPException, OSError) as e:
                        raise NetworkError("connection", f"The connection broke: {e}") from None
                    if not chunk:
                        break
                    out.write(chunk)
                    f.done += len(chunk)
                    if verify == "crc" and f.crc:
                        crc = zlib.crc32(chunk, crc)
                    self.throttle.wait(len(chunk))
                    self._tick(job)
                out.flush()
                os.fsync(out.fileno())
        finally:
            resp.close()
        size = os.path.getsize(f.part)
        if f.size and size != f.size:
            if size < f.size:
                raise NetworkError("connection", "The connection ended before the file was complete")
            self._drop_part(f)
            raise RommError("server", f"{f.name} is larger than RomM says; downloading it again may help")
        if verify == "crc" and f.crc and not crc_matches(f.crc, crc):
            inner = zip_member_crcs(f.part) if f.name.lower().endswith(".zip") else None
            if inner is not None and any(crc_matches(f.crc, m) for m in inner):
                log.info("%s matches RomM's checksum of a file inside the zip", f.name)
            else:
                log.warn("%s: RomM lists crc %s, the download has %08x%s", f.name, f.crc, crc,
                         " and its files have " + ", ".join("%08x" % m for m in sorted(inner)[:4]) if inner else "")
                self._drop_part(f)
                raise RommError("server", f"{f.name} did not match RomM's checksum; try the download again")
        os.replace(f.part, f.dest)
        f.done = size

    def _tick(self, job: Job) -> None:
        now = time.monotonic()
        if now - job._since >= 1.0:
            done = job.done
            job.speed = (done - job._since_bytes) / (now - job._since)
            job._since, job._since_bytes = now, done
        self._emit(job)

    def _discard_parts(self, job: Job) -> None:
        for f in job.files:
            try:
                os.unlink(f.part)
            except OSError:
                pass
            f.done = 0

    # -- after the last file ----------------------------------------------------------------------------------------

    def _finish(self, job: Job, layout: Layout) -> None:
        rom = job.rom
        placed = [(next((x for x in rom["files"] if int(x["id"]) == f.id), {"id": f.id, "file_name": f.name}), Path(f.dest))
                  for f in job.files]
        paths_ = [d for _, d in placed]
        if self.settings.get("library.extract_zip"):
            paths_ = self._extract_zips(job, layout, paths_)
        main = choose_main_file(paths_)
        entry = make_entry(rom, layout, placed, main)
        if len(paths_) != len(placed) or any(a != b for a, b in zip(paths_, [d for _, d in placed])):
            entry["files"] = [{"id": None, "path": str(p), "size": p.stat().st_size if p.exists() else 0, "name": p.name}
                              for p in paths_]
            entry["size"] = sum(x["size"] for x in entry["files"])
        self.manifest.put(entry)
        log.info("installed %s (%s)", entry["name"], human_bytes(entry["size"]))

    def _extract_zips(self, job: Job, layout: Layout, paths_: list[Path]) -> list[Path]:
        out: list[Path] = []
        for p in paths_:
            if p.suffix.lower() != ".zip" or not zipfile.is_zipfile(p):
                out.append(p)
                continue
            with zipfile.ZipFile(p) as z:
                names = [n for n in z.namelist() if not n.endswith("/")]
                target = p.parent if len(names) == 1 else p.with_suffix("")
                target.mkdir(parents=True, exist_ok=True)
                for member in z.infolist():
                    if member.is_dir():
                        continue
                    dest = (target / member.filename).resolve()
                    if not is_within(target, dest):
                        raise ValueError("The archive has a path that leaves its folder")
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with z.open(member) as src, open(dest, "wb") as dst:
                        shutil.copyfileobj(src, dst, CHUNK)
                    out.append(dest)
            p.unlink()
        return out

    # -- removing a game ----------------------------------------------------------------------------------------------

    def uninstall(self, rom_id: int) -> dict:
        """Deletes the game's files from the library. Saves and memory cards are never touched."""
        entry = self.manifest.get(rom_id)
        root = Path(self.settings.get("library.root"))
        removed = 0
        if entry:
            top = entry.get("top")
            targets = [Path(f["path"]) for f in entry.get("files", [])]
            for t in targets:
                if is_within(root, t) and t.is_file():
                    try:
                        t.unlink()
                        removed += 1
                    except OSError as e:
                        log.warn("could not delete %s: %s", t, e)
            if top and is_within(root, top) and Path(top).is_dir() and Path(top) != Path(entry.get("dir", "")):
                shutil.rmtree(top, ignore_errors=True)
            self.manifest.remove(rom_id)
        with self._cv:
            j = self.jobs.get(rom_id)
            if j and j.state in (DONE, ERROR, CANCELLED, PAUSED):
                del self.jobs[rom_id]
                if rom_id in self.order:
                    self.order.remove(rom_id)
        self._save()
        return {"removed": removed}
