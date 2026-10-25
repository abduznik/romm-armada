# SPDX-License-Identifier: MIT
"""Small helpers with no dependencies on the rest of the plugin."""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or now_utc()).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        v = value.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(v)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def ts_to_iso(ts: float) -> str:
    return iso(datetime.fromtimestamp(ts, tz=timezone.utc))


def human_bytes(n: float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


_BAD = re.compile(r'[\x00-\x1f<>:"|?*\\]')


def safe_segment(name: str, fallback: str = "_") -> str:
    """One path segment that cannot climb out of its folder or upset a FAT/exFAT card."""
    name = _BAD.sub("_", name.replace("/", "_")).strip().rstrip(".")
    if name in ("", ".", ".."):
        return fallback
    data = name.encode("utf-8")
    if len(data) > 240:  # ext4 and exFAT both stop at 255 bytes; keep room for ".part"
        stem, ext = os.path.splitext(name)
        keep = 240 - len(ext.encode("utf-8"))
        name = data[:keep].decode("utf-8", "ignore").rstrip() + ext
    return name


def safe_relpath(rel: str) -> Path:
    """A relative path from the server, cleaned segment by segment. Raises ValueError if it is empty."""
    parts = [safe_segment(p) for p in re.split(r"[\\/]+", rel) if p not in ("", ".", "..")]
    if not parts:
        raise ValueError("empty path")
    return Path(*parts)


def is_within(root: Path | str, path: Path | str) -> bool:
    root = os.path.realpath(root)
    path = os.path.realpath(path)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def atomic_write(path: Path | str, data: bytes, mode: int = 0o644) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path: Path | str, default: Any = None) -> Any:
    try:
        with open(path, "rb") as f:
            return json.loads(f.read().decode("utf-8"))
    except FileNotFoundError:
        return default
    except (ValueError, OSError):
        # A file that will not parse is kept aside, not overwritten, so nothing is lost silently.
        try:
            os.replace(path, f"{path}.broken-{int(time.time())}")
        except OSError:
            pass
        return default


def write_json(path: Path | str, obj: Any, mode: int = 0o644) -> None:
    atomic_write(path, (json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8"), mode)


def free_bytes(path: Path | str) -> int:
    """Free space on the volume `path` is (or would be) on."""
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    try:
        return shutil.disk_usage(p).free
    except OSError:
        return 0


def volume_info(path: Path | str) -> dict:
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    try:
        u = shutil.disk_usage(p)
        return {"total": u.total, "free": u.free}
    except OSError:
        return {"total": 0, "free": 0}


_MOUNT_ROOTS = ("/run/media/", "/media/", "/mnt/")


def removable_volumes() -> list[dict]:
    """Mounted volumes under the usual removable-media folders, for offering an SD card as a location."""
    out: list[dict] = []
    seen: set[str] = set()
    try:
        with open("/proc/self/mountinfo", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return out
    for line in lines:
        fields = line.split(" ")
        if len(fields) < 5:
            continue
        mount = fields[4].replace("\\040", " ")
        if not mount.startswith(_MOUNT_ROOTS) or mount in seen:
            continue
        seen.add(mount)
        info = volume_info(mount)
        if info["total"] <= 0:
            continue
        out.append({"path": mount, "name": os.path.basename(mount.rstrip("/")) or mount, **info})
    return out


class Throttle:
    """A bandwidth cap shared by the download threads. `rate` is bytes per second, 0 for none."""

    def __init__(self, rate: int = 0) -> None:
        self.rate = rate
        self._lock = threading.Lock()
        self._next = time.monotonic()

    def wait(self, nbytes: int) -> None:
        rate = self.rate
        if rate <= 0:
            return
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next)
            self._next = start + nbytes / rate
            delay = start - now
        if delay > 0:
            time.sleep(min(delay, 5.0))


def chunks(seq: list, size: int) -> Iterable[list]:
    for i in range(0, len(seq), size):
        yield seq[i:i + size]
