# SPDX-License-Identifier: MIT
"""The local library: where each ROM goes on disk, and the manifest of what the plugin has installed.

Everything lives under one dedicated root (Settings > Library > Download location), in a folder per
platform. The folder names follow ES-DE's system names by default, so ES-DE finds the games with no
setup; they can follow RomM's own folder names instead, or be overridden platform by platform.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

from . import paths
from .config import expand_path
from .util import iso, now_utc, read_json, safe_relpath, safe_segment, write_json

# RomM platform slug -> ES-DE system folder, where they differ. Anything not listed keeps RomM's folder name.
ESDE_NAMES = {
    "ps": "psx", "psx": "psx", "ps2": "ps2", "ps3": "ps3", "psp": "psp", "psvita": "psvita", "vita": "psvita",
    "ngc": "gc", "gamecube": "gc", "nintendo-gamecube": "gc", "wii": "wii", "wiiu": "wiiu",
    "switch": "switch", "nintendo-switch": "switch", "3ds": "n3ds", "nintendo-3ds": "n3ds", "n3ds": "n3ds",
    "nds": "nds", "nintendo-ds": "nds", "n64": "n64", "nintendo-64": "n64",
    "nes": "nes", "famicom": "famicom", "snes": "snes", "sfam": "sfc", "gb": "gb", "gbc": "gbc", "gba": "gba",
    "genesis-slash-megadrive": "megadrive", "genesis": "genesis", "sega-mega-drive": "megadrive", "mega-drive": "megadrive",
    "sms": "mastersystem", "sega-master-system": "mastersystem", "gamegear": "gamegear", "sega-game-gear": "gamegear",
    "dc": "dreamcast", "dreamcast": "dreamcast", "saturn": "saturn", "sega-saturn": "saturn",
    "segacd": "segacd", "sega-cd": "segacd", "sega32": "sega32x", "sega-32x": "sega32x",
    "tg16": "tg16", "turbografx16--1": "tg16", "pc-engine": "pcengine", "pcfx": "pcfx",
    "neo-geo-pocket": "ngp", "neo-geo-pocket-color": "ngpc", "neogeoaes": "neogeo", "neo-geo": "neogeo",
    "lynx": "atarilynx", "atari-lynx": "atarilynx", "jaguar": "atarijaguar", "atari-jaguar": "atarijaguar",
    "atari2600": "atari2600", "atari7800": "atari7800", "3do": "3do", "arcade": "arcade", "dos": "dos",
    "xbox": "xbox", "xbox360": "xbox360", "wonderswan": "wonderswan", "wonderswan-color": "wonderswancolor",
}

# Files handed to an emulator first, best first
MAIN_FILE_ORDER = (".m3u", ".cue", ".gdi", ".chd", ".iso", ".cso", ".zso", ".gz", ".img", ".mdf", ".nrg", ".bin", ".elf")

SKIPPED_CATEGORIES = {"manual", "walkthrough", "soundtrack", "screenshot"}


def platform_dir_name(style: str, overrides: dict, platform_slug: str, fs_slug: str) -> str:
    for key in (fs_slug, platform_slug):
        if key and key in overrides:
            return safe_segment(overrides[key])
    if style == "esde":
        for key in (platform_slug, fs_slug):
            if key and key.lower() in ESDE_NAMES:
                return ESDE_NAMES[key.lower()]
    return safe_segment(fs_slug or platform_slug or "unknown")


def pick_files(rom: dict, include_extras: bool) -> list[dict]:
    """The server's files for this ROM that the plugin downloads."""
    files = [f for f in (rom.get("files") or []) if (f.get("category") or "game") not in SKIPPED_CATEGORIES]
    if not include_extras:
        files = [f for f in files if (f.get("category") or "game") == "game"]
    return files


def rom_folder_style(rom: dict) -> bool:
    """True when the ROM is a folder on the server (several files, or one inside a folder)."""
    return bool(rom.get("has_multiple_files") or rom.get("has_nested_single_file"))


def relative_in_rom(rom: dict, f: dict) -> str:
    """Path of file `f` inside a folder ROM: its full path on the server minus the ROM folder."""
    full = (f.get("full_path") or "").strip("/")
    base = (rom.get("full_path") or "").strip("/")
    if base and full.startswith(base + "/"):
        return full[len(base) + 1:]
    return f.get("file_name") or "file"


class Layout:
    """Maps ROMs to paths for the current settings."""

    def __init__(self, root: str, style: str, overrides: dict) -> None:
        self.root = Path(expand_path(root))   # never a relative "~/ROMs"
        self.style = style
        self.overrides = overrides

    def platform_dir(self, rom: dict) -> Path:
        name = platform_dir_name(self.style, self.overrides, rom.get("platform_slug", ""), rom.get("platform_fs_slug", ""))
        return self.root / name

    def top(self, rom: dict) -> Path:
        """The file (or folder) that stands for the ROM in the platform folder."""
        return self.platform_dir(rom) / safe_segment(rom.get("fs_name") or rom.get("name") or f"rom-{rom.get('id')}")

    def destination(self, rom: dict, f: dict) -> Path:
        if rom_folder_style(rom):
            return self.top(rom) / safe_relpath(relative_in_rom(rom, f))
        return self.platform_dir(rom) / safe_segment(f.get("file_name") or rom.get("fs_name") or "rom")


def choose_main_file(paths_: list[Path]) -> Path | None:
    if not paths_:
        return None
    for ext in MAIN_FILE_ORDER:
        hits = sorted(p for p in paths_ if p.suffix.lower() == ext)
        if hits:
            # prefer the shallowest: a playlist or sheet at the top of the ROM folder
            return min(hits, key=lambda p: (len(p.parts), str(p)))
    return sorted(paths_)[0]


class Manifest:
    """installed.json: what the plugin put on disk, by RomM ROM id."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else paths.state_dir() / "installed.json"
        self._lock = threading.RLock()
        self._games: dict[str, dict] = {}
        self.load()

    def load(self) -> None:
        with self._lock:
            doc = read_json(self.path, {})
            self._games = doc.get("games", {}) if isinstance(doc, dict) else {}

    def save(self) -> None:
        with self._lock:
            write_json(self.path, {"version": 1, "games": self._games})

    def get(self, rom_id: int) -> dict | None:
        with self._lock:
            g = self._games.get(str(rom_id))
            return dict(g) if g else None

    def put(self, entry: dict) -> None:
        with self._lock:
            self._games[str(entry["rom_id"])] = entry
            self.save()

    def update(self, rom_id: int, **fields: Any) -> dict | None:
        with self._lock:
            g = self._games.get(str(rom_id))
            if not g:
                return None
            g.update(fields)
            self.save()
            return dict(g)

    def remove(self, rom_id: int) -> dict | None:
        with self._lock:
            g = self._games.pop(str(rom_id), None)
            if g is not None:
                self.save()
            return g

    def all(self) -> list[dict]:
        with self._lock:
            return [dict(g) for g in self._games.values()]

    def by_paths(self, paths: list[str]) -> list[tuple[str, dict]]:
        """The installed games that any of `paths` belongs to, each with the path that matched: what a process holds open."""
        reals = [(p, os.path.realpath(p)) for p in paths]
        with self._lock:
            games = [(os.path.realpath(g["main_file"]) if g.get("main_file") else None,
                      os.path.realpath(g["top"]) if g.get("top") else None, g) for g in self._games.values()]
            out: list[tuple[str, dict]] = []
            for p, real in reals:
                for main, top, g in games:
                    if (main and real == main) or (top and (real == top or real.startswith(top + os.sep))):
                        out.append((p, dict(g)))
                        break
        return out

    def by_path(self, path: str) -> dict | None:
        """The installed game whose files include `path` (what a running emulator was started with)."""
        real = os.path.realpath(path)
        with self._lock:
            for g in self._games.values():
                main = g.get("main_file")
                if main and os.path.realpath(main) == real:
                    return dict(g)
                top = g.get("top")
                if top and (real == os.path.realpath(top) or real.startswith(os.path.realpath(top) + os.sep)):
                    return dict(g)
        return None


def make_entry(rom: dict, layout: Layout, files: list[tuple[dict, Path]], main: Path | None) -> dict:
    total = 0
    items = []
    for f, dest in files:
        size = dest.stat().st_size if dest.exists() else int(f.get("file_size_bytes") or 0)
        total += size
        items.append({"id": f.get("id"), "path": str(dest), "size": size, "name": f.get("file_name")})
    return {
        "rom_id": rom["id"], "name": rom.get("name") or rom.get("fs_name") or f"ROM {rom['id']}",
        "fs_name": rom.get("fs_name"), "platform_slug": rom.get("platform_slug"),
        "platform_fs_slug": rom.get("platform_fs_slug"), "platform_name": rom.get("platform_display_name") or rom.get("platform_name"),
        "platform_id": rom.get("platform_id"), "dir": str(layout.platform_dir(rom)), "top": str(layout.top(rom)),
        "main_file": str(main) if main else None, "files": items, "size": total,
        "installed_at": iso(now_utc()), "title_id": rom.get("title_id"),
    }
