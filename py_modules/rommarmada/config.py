# SPDX-License-Identifier: MIT
"""Settings: a flat schema of dotted keys, each with a type, a default and a check.

The UI sends partial updates ({"saves": {"slot": "armadaos"}}); every leaf is validated before any
of them is applied, so a bad value never half-saves. The token never leaves the backend.
"""
from __future__ import annotations

import copy
import os
import socket
import threading
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from . import paths
from .util import read_json, write_json

SCHEMA_VERSION = 3

# What this plugin's saves are called on RomM: the slot they go to (as Freegosy's go to `freegosy`) and the emulator tag.
# Saves from any other slot are still pulled; this only names what this device writes.
DEFAULT_SLOT = "armadaos"
OLD_DEFAULT_SLOT = "autosave"           # versions 0.1 and 0.2 wrote this into the settings file

FILE_CATEGORIES_EXTRAS = ("update", "dlc", "patch", "translation", "hack", "mod", "demo", "prototype", "cheat")


class SettingsError(ValueError):
    pass


def _bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    raise SettingsError("must be true or false")


def _int_range(lo: int, hi: int) -> Callable[[Any], int]:
    def check(v: Any) -> int:
        if isinstance(v, bool) or not isinstance(v, (int, float)) or int(v) != v:
            raise SettingsError("must be a whole number")
        if not lo <= int(v) <= hi:
            raise SettingsError(f"must be between {lo} and {hi}")
        return int(v)
    return check


def _choice(*options: str) -> Callable[[Any], str]:
    def check(v: Any) -> str:
        if v not in options:
            raise SettingsError("must be one of: " + ", ".join(options))
        return v
    return check


def _text(maxlen: int = 255, allow_empty: bool = True) -> Callable[[Any], str]:
    def check(v: Any) -> str:
        if not isinstance(v, str):
            raise SettingsError("must be text")
        v = v.strip()
        if not v and not allow_empty:
            raise SettingsError("must not be empty")
        if len(v) > maxlen:
            raise SettingsError(f"is longer than {maxlen} characters")
        if any(ord(c) < 32 for c in v):
            raise SettingsError("has control characters")
        return v
    return check


def expand_path(value: str) -> str:
    value = value.strip()
    if value == "~" or value.startswith("~/"):
        value = str(paths.user_home()) + value[1:]
    return value


def _path(allow_empty: bool = True) -> Callable[[Any], str]:
    base = _text(4096, allow_empty)

    def check(v: Any) -> str:
        v = expand_path(base(v))
        if v and not os.path.isabs(v):
            raise SettingsError("must be an absolute path")
        return os.path.normpath(v) if v else v
    return check


def normalize_server_url(value: str) -> str:
    """http://host:port with no trailing slash and no /api suffix; a bare host gets http://."""
    v = value.strip()
    if not v:
        return ""
    if "://" not in v:
        v = "http://" + v
    parts = urlsplit(v)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise SettingsError("must be an http:// or https:// address")
    path = parts.path.rstrip("/")
    if path.endswith("/api"):
        path = path[:-4]
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def _url(v: Any) -> str:
    if not isinstance(v, str):
        raise SettingsError("must be text")
    return normalize_server_url(v)


def _platform_dirs(v: Any) -> dict:
    if not isinstance(v, dict):
        raise SettingsError("must be a map of platform to folder name")
    out = {}
    for key, name in v.items():
        if not isinstance(key, str) or not isinstance(name, str) or not key.strip():
            raise SettingsError("holds a bad entry")
        name = name.strip()
        if not name:
            continue  # an empty name removes the override
        if "/" in name or "\\" in name or name in (".", "..") or any(ord(c) < 32 for c in name):
            raise SettingsError(f"folder name '{name}' is not a plain folder name")
        out[key.strip()] = name
    return out


def default_device_name() -> str:
    try:
        host = socket.gethostname().split(".")[0] or "handheld"
    except OSError:
        host = "handheld"
    return f"{host} (RomM Armada)"


# dotted key -> (default, check, secret)
SCHEMA: dict[str, tuple[Any, Callable[[Any], Any], bool]] = {
    "server.url": ("", _url, False),
    "server.verify_tls": (True, _bool, False),

    "auth.token": ("", _text(4096), True),
    "auth.device_id": ("", _text(128), False),
    "auth.device_name": ("", _text(80), False),
    "auth.username": ("", _text(128), False),
    "auth.method": ("", _choice("", "pairing", "token", "password"), False),

    "library.root": ("~/ROMs", _path(allow_empty=False), False),
    "library.folder_style": ("esde", _choice("esde", "romm"), False),
    "library.platform_dirs": ({}, _platform_dirs, False),
    "library.include_extras": (True, _bool, False),
    "library.verify": ("crc", _choice("crc", "size", "none"), False),
    "library.max_concurrent": (1, _int_range(1, 3), False),
    "library.speed_limit_kbs": (0, _int_range(0, 1_000_000), False),   # kilobytes per second, 0 = unlimited
    "library.min_free_mb": (512, _int_range(0, 1_000_000), False),
    "library.extract_zip": (False, _bool, False),
    "library.resume_on_start": (True, _bool, False),
    "library.remote_installs": (False, _bool, False),

    "saves.enabled": (True, _bool, False),
    "saves.slot": (DEFAULT_SLOT, _text(64, allow_empty=False), False),
    "saves.format": ("folders", _choice("auto", "folders", "card"), False),
    "saves.push_on_exit": (True, _bool, False),
    "saves.pull_on_startup": (True, _bool, False),
    "saves.pull_when_idle": (True, _bool, False),
    "saves.pull_interval_min": (30, _int_range(0, 1440), False),
    "saves.conflict": ("ask", _choice("ask", "server", "device", "newest"), False),
    "saves.card_type": ("folder", _choice("folder", "auto"), False),
    "saves.backups": (True, _bool, False),
    "saves.backup_keep": (5, _int_range(1, 50), False),
    "saves.autocleanup": (True, _bool, False),
    "saves.autocleanup_limit": (10, _int_range(1, 100), False),
    "saves.playtime": (True, _bool, False),
    "saves.notify": (True, _bool, False),

    "emulators.ps2.emulator": ("auto", _choice("auto", "armsx2", "pcsx2"), False),
    "emulators.ps2.data_dir": ("", _path(), False),
    "emulators.ps2.executable": ("", _path(), False),

    "advanced.request_timeout": (30, _int_range(5, 300), False),
    "advanced.debug": (False, _bool, False),
}


def _flatten(d: dict, prefix: str = "") -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if f"{key}" in SCHEMA:
            flat[key] = v
        elif isinstance(v, dict) and any(s.startswith(key + ".") for s in SCHEMA):
            flat.update(_flatten(v, key + "."))
        else:
            flat[key] = v  # unknown: reported by the caller
    return flat


def _nest(flat: dict[str, Any]) -> dict:
    out: dict = {}
    for key, value in flat.items():
        node = out
        parts = key.split(".")
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = value
    return out


class Settings:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else paths.settings_dir() / "settings.json"
        self._lock = threading.RLock()
        # defaults go through the same checks as typed values, so "~/ROMs" is the real home folder from the start
        self._values: dict[str, Any] = {k: v[1](copy.deepcopy(v[0])) for k, v in SCHEMA.items()}
        self.loaded_from_disk = False

    # -- persistence -----------------------------------------------------------------------

    def load(self) -> None:
        migrated = False
        with self._lock:
            raw = read_json(self.path, None)
            if isinstance(raw, dict):
                self.loaded_from_disk = True
                for key, value in _flatten(raw).items():
                    if key not in SCHEMA:
                        continue
                    try:
                        self._values[key] = SCHEMA[key][1](value)
                    except SettingsError:
                        pass  # keep the default for a value that no longer validates
                version = raw.get("version")
                version = version if isinstance(version, int) else 1
                if version < 2 and self._values["saves.slot"] == OLD_DEFAULT_SLOT:
                    self._values["saves.slot"] = DEFAULT_SLOT      # the old default, never chosen: use the new name
                    migrated = True
                if version < 3 and self._values["saves.format"] == "auto":
                    self._values["saves.format"] = "folders"       # 0.3.0 saved "automatic" as its default; folders are the default now
                    migrated = True
            if not self._values["auth.device_name"]:
                self._values["auth.device_name"] = default_device_name()
        if migrated:
            self.save()

    def save(self) -> None:
        with self._lock:
            doc = _nest(copy.deepcopy(self._values))
            doc["version"] = SCHEMA_VERSION
            write_json(self.path, doc, mode=0o600)  # holds the token

    # -- access ----------------------------------------------------------------------------

    def get(self, key: str) -> Any:
        with self._lock:
            return copy.deepcopy(self._values[key])

    def update(self, patch: dict, persist: bool = True) -> tuple[dict, dict]:
        """Applies a nested patch. Returns (changed, errors); nothing is applied when there are errors."""
        flat = _flatten(patch)
        errors: dict[str, str] = {}
        checked: dict[str, Any] = {}
        for key, value in flat.items():
            if key not in SCHEMA:
                errors[key] = "unknown setting"
                continue
            try:
                checked[key] = SCHEMA[key][1](value)
            except SettingsError as e:
                errors[key] = str(e)
        if errors:
            return {}, errors
        with self._lock:
            changed = {k: v for k, v in checked.items() if self._values.get(k) != v}
            self._values.update(changed)
            if changed and persist:
                self.save()
        return _nest(changed), {}

    # -- views -----------------------------------------------------------------------------

    def as_dict(self, include_secrets: bool = False) -> dict:
        with self._lock:
            flat = {k: copy.deepcopy(v) for k, v in self._values.items() if include_secrets or not SCHEMA[k][2]}
        doc = _nest(flat)
        doc.setdefault("auth", {})["signed_in"] = bool(self._values["auth.token"])
        return doc

    @property
    def signed_in(self) -> bool:
        return bool(self.get("server.url")) and bool(self.get("auth.token"))

    def clear_auth(self) -> None:
        self.update({"auth": {"token": "", "device_id": "", "username": "", "method": ""}})
