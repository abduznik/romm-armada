# SPDX-License-Identifier: MIT
"""What the plugin knows about each emulator: where its data lives, which sigil layout reads its saves,
how to tell it is running. PlayStation 2 first: ARMSX2 (the Arm64 PCSX2 fork) and PCSX2 itself.

Adding an emulator is a new profile here and a layout id from sigil (docs/platforms/README.md in
argosy-sigil lists them). Nothing else in the plugin names an emulator.
"""
from __future__ import annotations

import glob
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import paths

# --- a tolerant INI reader (PCSX2.ini is `[Section]` / `Key = value`) ----------------------------


def parse_ini(text: str) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    section = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in ";#":
            continue
        if line.startswith("[") and "]" in line:
            section = line[1:line.index("]")].strip()
            out.setdefault(section, {})
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            out.setdefault(section, {})[k.strip()] = v.strip()
    return out


def read_ini(path: Path) -> dict[str, dict[str, str]]:
    try:
        return parse_ini(Path(path).read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return {}


def ini_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ("true", "1", "yes", "on")


# --- profiles -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Profile:
    id: str                       # the setting value and the key in the registry
    name: str                     # shown to the user
    romm_emulator: str            # what the `emulator` field of an uploaded save says
    platform_slugs: tuple         # RomM platform slugs this emulator plays
    sigil_platform: str           # sigil's slug for the system
    layout: str                   # sigil layout id
    compat: tuple                 # `emulator` values whose saves this emulator can load
    process_names: tuple          # lower-case program names, to tell it is running
    data_dirs: Callable[[], list]         # where its data folder may be, best first
    executables: Callable[[], list]       # where its program may be, best first
    default_args: tuple = ()      # for launching
    family: str = "pcsx2"
    extra: dict = field(default_factory=dict)


def _xdg_config() -> Path:
    x = os.environ.get("XDG_CONFIG_HOME", "")
    return Path(x) if x and os.path.isabs(x) else paths.user_home() / ".config"


def _armsx2_dirs() -> list[Path]:
    home = paths.user_home()
    return [_xdg_config() / "ARMSX2", home / ".config" / "ARMSX2",
            home / ".var" / "app" / "net.armsx2.ARMSX2" / "config" / "ARMSX2"]


def _pcsx2_dirs() -> list[Path]:
    home = paths.user_home()
    return [_xdg_config() / "PCSX2", home / ".config" / "PCSX2",
            home / ".var" / "app" / "net.pcsx2.PCSX2" / "config" / "PCSX2"]


def _which(*names: str) -> list[str]:
    found = []
    for d in os.environ.get("PATH", "").split(os.pathsep):
        for n in names:
            p = os.path.join(d, n)
            if d and os.path.isfile(p) and os.access(p, os.X_OK):
                found.append(p)
    return found


def _globs(*patterns: str) -> list[str]:
    out: list[str] = []
    for pat in patterns:
        out += sorted(glob.glob(pat))
    return out


def _armsx2_exes() -> list[str]:
    h = str(paths.user_home())
    return (_globs(f"{h}/Applications/ARMSX2*.AppImage", f"{h}/.local/share/applications/ARMSX2*.AppImage",
                   f"{h}/.local/bin/ARMSX2*.AppImage", f"{h}/bin/ARMSX2*.AppImage", f"{h}/Applications/armsx2*/armsx2-*")
            + _which("armsx2-qt", "armsx2-sdl", "armsx2")
            + _globs(f"{h}/.local/share/flatpak/exports/bin/net.armsx2.ARMSX2", "/var/lib/flatpak/exports/bin/net.armsx2.ARMSX2"))


def _pcsx2_exes() -> list[str]:
    h = str(paths.user_home())
    return (_globs(f"{h}/Applications/pcsx2*.AppImage", f"{h}/Applications/PCSX2*.AppImage", f"{h}/.local/bin/pcsx2*.AppImage")
            + _which("pcsx2-qt", "pcsx2", "PCSX2")
            + _globs(f"{h}/.local/share/flatpak/exports/bin/net.pcsx2.PCSX2", "/var/lib/flatpak/exports/bin/net.pcsx2.PCSX2"))


_PS2_COMPAT = ("armsx2_refresh", "armsx2", "pcsx2", "nethersx2", "aethersx2")

PROFILES: dict[str, Profile] = {
    "armsx2": Profile(
        id="armsx2", name="ARMSX2", romm_emulator="armsx2_refresh", platform_slugs=("ps2",), sigil_platform="ps2",
        layout="pcsx2_standalone", compat=_PS2_COMPAT,
        process_names=("armsx2-qt", "armsx2-sdl", "armsx2", "armsx2.appimage"),
        data_dirs=_armsx2_dirs, executables=_armsx2_exes, default_args=("-batch", "-fullscreen", "--"),
    ),
    "pcsx2": Profile(
        id="pcsx2", name="PCSX2", romm_emulator="pcsx2", platform_slugs=("ps2",), sigil_platform="ps2",
        layout="pcsx2_standalone", compat=_PS2_COMPAT,
        process_names=("pcsx2-qt", "pcsx2", "pcsx2.appimage"),
        data_dirs=_pcsx2_dirs, executables=_pcsx2_exes, default_args=("-batch", "-fullscreen", "--"),
    ),
}

# RomM platform slug -> setting group (the key under `emulators.` in the settings)
PLATFORM_GROUPS = {"ps2": "ps2"}


def group_for_platform(platform_slug: str) -> str | None:
    return PLATFORM_GROUPS.get((platform_slug or "").lower())


# --- finding what is installed ------------------------------------------------------------------


@dataclass
class Resolved:
    profile: Profile
    data_dir: Path
    ini: Path
    data_dir_exists: bool
    executable: str
    why: str                      # how the data folder was chosen: setting, found, default


def resolve_ps2(emulator_setting: str = "auto", data_dir_setting: str = "", exe_setting: str = "") -> Resolved:
    """Picks the PS2 emulator and its data folder: the settings win, then what exists, then ARMSX2's default."""
    order = ["armsx2", "pcsx2"] if emulator_setting in ("auto", "") else [emulator_setting]
    chosen: Profile | None = None
    folder: Path | None = None
    why = "default"
    if data_dir_setting:
        chosen = PROFILES[order[0]]
        folder, why = Path(data_dir_setting), "setting"
    else:
        for pid in order:
            for d in PROFILES[pid].data_dirs():
                if d.is_dir():
                    chosen, folder, why = PROFILES[pid], d, "found"
                    break
            if chosen:
                break
    if chosen is None:
        # nothing exists yet. Prefer the emulator whose program is installed; ARMSX2 on an Arm64 handheld otherwise
        for pid in order:
            if PROFILES[pid].executables():
                chosen = PROFILES[pid]
                break
        chosen = chosen or PROFILES[order[0]]
        folder = chosen.data_dirs()[0]
    assert folder is not None
    exes = [exe_setting] if exe_setting else chosen.executables()
    return Resolved(chosen, folder, folder / "inis" / "PCSX2.ini", folder.is_dir(), exes[0] if exes else "", why)


# --- PCSX2-family memory cards -----------------------------------------------------------------------

DEFAULT_CARDS = {1: "Mcd001.ps2", 2: "Mcd002.ps2"}


@dataclass
class Slot:
    number: int
    enabled: bool
    name: str
    path: Path
    kind: str                     # folder, file, missing, other
    size: int = 0


@dataclass
class CardConfig:
    memcards_dir: Path            # where the cards live
    rel_memcards: str | None      # relative to the data root when it is `memcards`-like, else None
    slots: list[Slot]
    folder_auto_manage: bool
    custom_dir: bool              # the memory card folder was moved somewhere other than <data>/memcards


def _classify(p: Path) -> tuple[str, int]:
    try:
        if p.is_dir():
            return "folder", 0
        if p.is_file():
            return "file", p.stat().st_size
        if p.exists():
            return "other", 0
    except OSError:
        pass
    return "missing", 0


def card_config(data_dir: Path, serial: str | None = None) -> CardConfig:
    """The memory card setup from PCSX2.ini, with the per-game override applied when `serial` has one."""
    ini = read_ini(data_dir / "inis" / "PCSX2.ini")
    folders = ini.get("Folders", {})
    raw = folders.get("MemoryCards", "memcards")
    mem = Path(raw) if os.path.isabs(raw) else data_dir / raw
    rel = raw if not os.path.isabs(raw) else None
    custom = (os.path.normpath(str(mem)) != os.path.normpath(str(data_dir / "memcards")))
    cards = ini.get("MemoryCards", {})
    override: dict[str, str] = {}
    if serial:
        for f in sorted(glob.glob(str(data_dir / "gamesettings" / f"{glob.escape(serial)}_*.ini"))):
            override = read_ini(Path(f)).get("MemoryCards", {})
            if override:
                break
    slots: list[Slot] = []
    for n in (1, 2):
        get = lambda key, default: override.get(key, cards.get(key, default))  # noqa: E731
        name = get(f"Slot{n}_Filename", DEFAULT_CARDS[n])
        enabled = ini_bool(get(f"Slot{n}_Enable", "true" if n == 1 else "false"), n == 1)
        p = mem / name if name else mem
        kind, size = _classify(p) if name else ("missing", 0)
        slots.append(Slot(n, enabled and bool(name), name, p, kind, size))
    manage = ini_bool(ini.get("EmuCore", {}).get("McdFolderAutoManage"), True)
    return CardConfig(mem, rel, slots, manage, custom)


def sigil_options(cfg: CardConfig) -> dict[str, str]:
    """The settings sigil needs to find the cards: only what differs from the defaults."""
    opts: dict[str, str] = {}
    for s in cfg.slots:
        if s.name and s.name != DEFAULT_CARDS[s.number]:
            opts[f"Slot{s.number}_Filename"] = s.name
    return opts


# --- running programs ------------------------------------------------------------------------------


@dataclass
class Instance:
    pid: int
    comm: str
    exe: str
    cmdline: list
    started: float                # seconds since boot, as /proc/<pid>/stat has it (0 if unknown)

    def rom_path(self) -> str | None:
        """The disc image the emulator was started with: after `--` if given, else the last argument that is a file."""
        args = self.cmdline[1:]
        if "--" in args:
            tail = args[args.index("--") + 1:]
            if tail:
                return tail[-1]
        for a in reversed(args):
            if not a.startswith("-") and os.path.exists(a):
                return a
        return None


def find_instances(process_names: tuple, proc_root: str = "/proc") -> list[Instance]:
    names = {n.lower() for n in process_names}
    found: list[Instance] = []
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return found
    for e in entries:
        if not e.isdigit():
            continue
        base = os.path.join(proc_root, e)
        try:
            with open(os.path.join(base, "comm"), encoding="utf-8", errors="replace") as f:
                comm = f.read().strip()
            with open(os.path.join(base, "cmdline"), "rb") as f:
                cmd = [a.decode("utf-8", "replace") for a in f.read().split(b"\0") if a]
            try:
                exe = os.readlink(os.path.join(base, "exe"))
            except OSError:
                exe = ""
        except OSError:
            continue
        cand = {comm.lower(), os.path.basename(exe).lower()}
        if cmd:
            cand.add(os.path.basename(cmd[0]).lower())
        if not (cand & names):
            continue
        started = 0.0
        try:
            with open(os.path.join(base, "stat"), encoding="utf-8", errors="replace") as f:
                stat = f.read()
            started = float(stat[stat.rindex(")") + 2:].split()[19])  # starttime, clock ticks since boot
        except (OSError, ValueError, IndexError):
            pass
        found.append(Instance(int(e), comm, exe, cmd, started))
    return found


def is_running(profile: Profile, proc_root: str = "/proc") -> bool:
    return bool(find_instances(profile.process_names, proc_root))


def all_ps2_process_names() -> tuple:
    return tuple(sorted({n for p in PROFILES.values() for n in p.process_names}))


def serial_from_path(rom: str | None) -> str | None:
    """A PS2 serial in a file name, e.g. 'SLUS-20152' or 'SLUS_201.52', when the name has one."""
    if not rom:
        return None
    m = re.search(r"\b([A-Z]{4})[-_ ]?(\d{3})[._]?(\d{2})\b", os.path.basename(rom).upper())
    return f"{m.group(1)}-{m.group(2)}{m.group(3)}" if m else None
