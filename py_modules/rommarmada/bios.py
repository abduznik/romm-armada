# SPDX-License-Identifier: MIT
"""Putting BIOS files where the emulator looks for them, from the firmware RomM keeps for the platform.

Only on request (a button), never by itself. ARMSX2 and PCSX2 find a BIOS on their own: when the one named in
their settings is missing they scan the BIOS folder for a valid image, so placing the file there is all that is
needed and the emulator's settings are left alone.

Rules:
- a file that is already there with RomM's content is skipped, also when it was renamed;
- a file with the same name but other content is kept unless the caller says to replace it, and the old one is
  copied to the plugin's backups first;
- a download is checked against the size and hashes RomM lists before it gets its final name, so a broken
  transfer never leaves a bad BIOS in the folder.
"""
from __future__ import annotations

import errno
import hashlib
import http.client
import os
import shutil
import time
import zlib
from pathlib import Path

from . import log
from .http import HttpError, NetworkError
from .romm import RommClient, RommError, wrap_error
from .util import free_bytes, human_bytes, safe_segment

CHUNK = 256 * 1024
MAX_FILE_BYTES = 64 * 1024 * 1024       # a PS2 BIOS is 4 to 8 MiB; this only keeps a wrong file from filling the disk
MARGIN_BYTES = 64 * 1024 * 1024         # left free after the files are in


class BiosError(Exception):
    """A downloaded file that cannot be used; the text finishes the sentence that starts with its name."""


# -- what the folder holds -----------------------------------------------------------------------------

_md5_cache: dict[tuple, str] = {}


def md5_of(path: Path) -> str:
    """MD5 of a file, remembered while the file stays as it is (the panel asks again on every refresh)."""
    try:
        st = path.stat()
    except OSError:
        return ""
    key = (str(path), st.st_size, st.st_mtime_ns)
    hit = _md5_cache.get(key)
    if hit:
        return hit
    h = hashlib.md5()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return ""
    if len(_md5_cache) > 256:
        _md5_cache.clear()
    _md5_cache[key] = h.hexdigest()
    return _md5_cache[key]


def _norm_hex(value) -> str:
    """A hash the way RomM may spell it (upper case, 0x, zero padded) reduced to something comparable."""
    text = str(value or "").strip().lower()
    if text.startswith("0x"):
        text = text[2:]
    return text.lstrip("0")


def check(fw: dict, directory: Path) -> tuple[str, str]:
    """(state, file name) for one of RomM's firmware records against a folder.

    installed: the file of that name has RomM's content. present: RomM's content is there under another name.
    different: a file of that name is there with other content. missing: nothing."""
    directory = Path(directory)
    name = safe_segment(fw["file_name"])
    want_size = int(fw.get("file_size_bytes") or 0)
    want_md5 = _norm_hex(fw.get("md5_hash"))
    try:
        entries = sorted(p for p in directory.iterdir() if p.is_file() and not p.name.startswith("."))
    except OSError:
        return "missing", name

    def same(p: Path) -> bool:
        try:
            if want_size and p.stat().st_size != want_size:
                return False
        except OSError:
            return False
        return not want_md5 or _norm_hex(md5_of(p)) == want_md5

    own = next((p for p in entries if p.name == name), None)
    if own is not None and same(own):
        return "installed", name
    if want_md5:
        for p in entries:
            if p.name != name and same(p):
                return "present", p.name
    return ("different" if own is not None else "missing"), name


def describe_items(items: list[dict], directory: Path) -> list[dict]:
    """The firmware RomM lists, as the panel shows it: verified ones first, each with what the folder says about it."""
    out = []
    for fw in items:
        if fw.get("missing_from_fs"):
            continue
        state, held_as = check(fw, directory)
        out.append({"id": fw["id"], "name": fw["file_name"], "size": int(fw.get("file_size_bytes") or 0),
                    "verified": bool(fw.get("is_verified")), "state": state, "held_as": held_as,
                    "md5": fw.get("md5_hash") or ""})
    out.sort(key=lambda i: (not i["verified"], i["name"].lower()))
    return out


# -- fetching ---------------------------------------------------------------------------------------------


def _fetch(client: RommClient, fw: dict, part: Path) -> None:
    """Downloads one firmware file into `part` and checks it. Raises BiosError, RommError or OSError."""
    md5, sha1, crc, size = hashlib.md5(), hashlib.sha1(), 0, 0
    try:
        resp = client.http.open("GET", client.firmware_path(fw), timeout=60)
    except (HttpError, NetworkError) as e:
        raise wrap_error(e) from None
    try:
        with open(part, "wb") as out:
            while True:
                try:
                    chunk = resp.read(CHUNK)
                except (http.client.HTTPException, OSError) as e:
                    raise RommError("network", f"The connection broke: {e}", net_kind="connection") from None
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_FILE_BYTES:
                    raise BiosError("is larger than any BIOS file can be")
                out.write(chunk)
                md5.update(chunk)
                sha1.update(chunk)
                crc = zlib.crc32(chunk, crc)
            out.flush()
            os.fsync(out.fileno())
    finally:
        resp.close()
    want = int(fw.get("file_size_bytes") or 0)
    if want and size < want:
        raise RommError("network", "The connection ended before the file was complete", net_kind="connection")
    if want and size > want:
        raise BiosError("is larger than RomM says it is")
    for field, got in (("md5_hash", md5.hexdigest()), ("sha1_hash", sha1.hexdigest()), ("crc_hash", format(crc & 0xFFFFFFFF, "x"))):
        if fw.get(field) and _norm_hex(fw[field]) != _norm_hex(got):
            raise BiosError("did not match RomM's checksum; try again")


def _explain(name: str, e: Exception) -> tuple[str, str]:
    """(message, kind) for a failed file."""
    if isinstance(e, RommError):
        text = {"not_found": "RomM does not have this file any more (scan the firmware again in RomM)",
                "forbidden": "this sign-in may not download firmware; pair the device again",
                "auth": "the sign-in was not accepted; pair the device again"}.get(e.kind, e.message)
        return f"{name}: {text}", e.kind
    if isinstance(e, BiosError):
        return f"{name} {e}", "bad_file"
    return f"{name}: {getattr(e, 'strerror', None) or e}", "disk"


def install(client: RommClient, items: list[dict], directory: Path, *, overwrite: bool, emulator_running: bool,
            backup_root: Path) -> dict:
    """Puts the given firmware (RomM's records) into `directory`.

    Returns {installed, skipped, conflicts, busy, failed}: names, except `failed` (name, error, kind). Raises OSError
    when the disk cannot take the files."""
    directory = Path(directory)
    report: dict = {"installed": [], "skipped": [], "conflicts": [], "busy": [], "failed": []}
    wanted = [fw for fw in items if not fw.get("missing_from_fs")]
    plan = [(fw, *check(fw, directory)) for fw in wanted]
    need = sum(int(fw.get("file_size_bytes") or 0) for fw, state, _ in plan if state in ("missing", "different"))
    if need and free_bytes(directory) < need + MARGIN_BYTES:
        raise OSError(errno.ENOSPC, f"Needs {human_bytes(need)} but only {human_bytes(free_bytes(directory))} is free")
    for fw, state, held_as in plan:
        name = safe_segment(fw["file_name"])
        if state in ("installed", "present"):
            report["skipped"].append(held_as)
            continue
        if state == "different":
            if not overwrite:
                report["conflicts"].append(name)
                continue
            if emulator_running:
                report["busy"].append(name)
                continue
        dest = directory / name
        part = directory / f".{name}.part"
        try:
            directory.mkdir(parents=True, exist_ok=True)
            _fetch(client, fw, part)
            if state == "different":
                backup_root.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dest, backup_root / f"{time.strftime('%Y%m%d-%H%M%S')}-{name}")
            os.replace(part, dest)
            report["installed"].append(name)
            log.info("BIOS file %s installed in %s", name, directory)
        except (BiosError, RommError, OSError) as e:
            try:
                part.unlink()
            except OSError:
                pass
            msg, kind = _explain(name, e)
            report["failed"].append({"name": name, "error": msg, "kind": kind})
            log.warn("BIOS file %s not installed: %s", name, msg)
            if isinstance(e, RommError) and e.kind in ("auth", "forbidden", "network"):
                break       # the next file would meet the same wall
    return report


def summarize(report: dict, directory: Path) -> str:
    parts = []
    if report["installed"]:
        n = len(report["installed"])
        parts.append(f"Installed {n} file{'s' if n != 1 else ''} in {directory}.")
    if report["skipped"] and not report["installed"] and not report["failed"] and not report["conflicts"]:
        parts.append("Everything is already there.")
    elif report["skipped"]:
        parts.append(f"{len(report['skipped'])} already there.")
    if report["conflicts"]:
        parts.append(f"{', '.join(report['conflicts'])}: a different file with that name is already there and was kept.")
    if report["busy"]:
        parts.append("Close the emulator to replace " + ", ".join(report["busy"]) + ".")
    for f in report["failed"]:
        parts.append(f["error"] + ".")
    return " ".join(parts) or "Nothing to do."
