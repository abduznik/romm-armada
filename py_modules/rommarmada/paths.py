# SPDX-License-Identifier: MIT
"""Where the plugin keeps things.

Inside Decky the loader exports the directories as environment variables before it imports main.py,
so nothing here needs the `decky` module (and the tests can run without it). `ROMM_ARMADA_HOME`
moves everything under one folder, which the tests and the command line tool use.
"""
from __future__ import annotations

import os
from pathlib import Path


def _env(name: str) -> str:
    return os.environ.get(name, "")


def user_home() -> Path:
    return Path(_env("DECKY_USER_HOME") or _env("HOME") or os.path.expanduser("~"))


def plugin_dir() -> Path:
    """The folder holding main.py, bin/ and dist/."""
    env = _env("DECKY_PLUGIN_DIR")
    return Path(env) if env else Path(__file__).resolve().parents[2]


def _under_override(sub: str) -> Path | None:
    base = _env("ROMM_ARMADA_HOME")
    return Path(base) / sub if base else None


def settings_dir() -> Path:
    return _under_override("settings") or Path(_env("DECKY_PLUGIN_SETTINGS_DIR") or user_home() / ".config" / "romm-armada")


def runtime_dir() -> Path:
    return _under_override("data") or Path(_env("DECKY_PLUGIN_RUNTIME_DIR") or user_home() / ".local" / "share" / "romm-armada")


def log_dir() -> Path:
    return _under_override("logs") or Path(_env("DECKY_PLUGIN_LOG_DIR") or runtime_dir() / "logs")


def state_dir() -> Path:
    """Per-game sync state, the manifest of installed games and the download queue."""
    return settings_dir() / "state"


def backups_dir() -> Path:
    return runtime_dir() / "backups"


def cache_dir() -> Path:
    return runtime_dir() / "cache"


def helper_dir() -> Path:
    return plugin_dir() / "bin"
