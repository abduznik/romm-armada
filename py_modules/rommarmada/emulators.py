# SPDX-License-Identifier: MIT
"""What the plugin knows about each emulator: where its data lives, which sigil layout reads its saves,
how to tell it is running. PlayStation 2 first: ARMSX2 (the Arm64 PCSX2 fork) and PCSX2 itself, then RetroArch for the
8-bit and 16-bit systems whose cores keep one `<game>.srm` beside the other saves.

Adding an emulator is a new profile here and a layout id from sigil (docs/platforms/README.md in
argosy-sigil lists them).
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

# --- RetroArch ---------------------------------------------------------------------------------------

# RomM platform slug -> the platform name sigil knows, or "" where sigil reads nothing from the game. These are the systems
# whose libretro cores keep their save as `<game>.srm` (and `<game>.rtc` for a clock) in RetroArch's save folder, which
# is what sigil's default libretro row describes. Systems whose cores keep memory cards or folders (PlayStation, Nintendo
# DS, Nintendo 64, Dreamcast) are not here: they need a row per core.
RETROARCH_SYSTEMS = {
    "gb": "gb", "gbc": "gbc", "gba": "", "nes": "", "famicom": "", "snes": "", "sfam": "",
    "genesis-slash-megadrive": "", "genesis": "", "sega-mega-drive": "", "mega-drive": "",
    "sms": "", "sega-master-system": "", "gamegear": "", "sega-game-gear": "",
}


def _retroarch_dirs() -> list[Path]:
    home = paths.user_home()
    return [_xdg_config() / "retroarch", home / ".config" / "retroarch",
            home / ".var" / "app" / "org.libretro.RetroArch" / "config" / "retroarch", home / ".retroarch"]


def _retroarch_exes() -> list[str]:
    h = str(paths.user_home())
    return (_globs(f"{h}/Applications/RetroArch*.AppImage", f"{h}/Applications/retroarch*.AppImage",
                   f"{h}/.local/bin/RetroArch*.AppImage", f"{h}/Applications/RetroArch*/retroarch", f"{h}/Applications/RetroArch*/RetroArch*")
            + _which("retroarch", "RetroArch")
            + _globs(f"{h}/.local/share/flatpak/exports/bin/org.libretro.RetroArch", "/var/lib/flatpak/exports/bin/org.libretro.RetroArch"))


PROFILES["retroarch"] = Profile(
    id="retroarch", name="RetroArch", romm_emulator="retroarch", platform_slugs=tuple(RETROARCH_SYSTEMS), sigil_platform="",
    layout="libretro", compat=("retroarch",), process_names=("retroarch", "retroarch.appimage", "retroarch-bin"),
    data_dirs=_retroarch_dirs, executables=_retroarch_exes, default_args=(), family="libretro",
)

# --- Dolphin -----------------------------------------------------------------------------------------

DOLPHIN_SLUGS = ("gc", "ngc", "gamecube", "nintendo-gamecube")


def _xdg_data() -> Path:
    x = os.environ.get("XDG_DATA_HOME", "")
    return Path(x) if x and os.path.isabs(x) else paths.user_home() / ".local" / "share"


def _dolphin_dirs() -> list[Path]:
    """Dolphin's User folder (the one with GC/ and Wii/), best first. The Flatpak keeps it under its own data folder."""
    home = paths.user_home()
    return [home / ".var" / "app" / "org.DolphinEmu.dolphin-emu" / "data" / "dolphin-emu", _xdg_data() / "dolphin-emu",
            home / ".local" / "share" / "dolphin-emu", home / ".dolphin-emu"]


def _dolphin_exes() -> list[str]:
    h = str(paths.user_home())
    return (_globs(f"{h}/Applications/Dolphin*.AppImage", f"{h}/Applications/dolphin*.AppImage")
            + _which("dolphin-emu", "dolphin-emu-nogui")
            + _globs(f"{h}/.local/share/flatpak/exports/bin/org.DolphinEmu.dolphin-emu",
                     "/var/lib/flatpak/exports/bin/org.DolphinEmu.dolphin-emu"))


PROFILES["dolphin"] = Profile(
    id="dolphin", name="Dolphin", romm_emulator="dolphin", platform_slugs=DOLPHIN_SLUGS, sigil_platform="gamecube",
    layout="dolphin_standalone", compat=("dolphin",), process_names=("dolphin-emu", "dolphin-emu-nogui", "dolphin-emu.appimage"),
    data_dirs=_dolphin_dirs, executables=_dolphin_exes, default_args=("-b", "-e"), family="dolphin",
)


def dolphin_options(data_dir: Path) -> dict[str, str]:
    """The Dolphin.ini settings that decide where GameCube saves are (a GCI folder or a raw card, and its size)."""
    data_dir = Path(data_dir)
    home = paths.user_home()
    cands = [data_dir / "Config" / "Dolphin.ini", data_dir.parent.parent / "config" / "dolphin-emu" / "Dolphin.ini",
             _xdg_config() / "dolphin-emu" / "Dolphin.ini", home / ".config" / "dolphin-emu" / "Dolphin.ini"]
    for c in cands:
        core = read_ini(c).get("Core")
        if core:
            return {k: core[k] for k in ("SlotA", "MemoryCardSize") if core.get(k) not in (None, "")}
    return {}


def resolve_dolphin(data_dir_setting: str = "", exe_setting: str = "") -> Resolved:
    profile = PROFILES["dolphin"]
    if data_dir_setting:
        folder, why = Path(data_dir_setting), "setting"
    else:
        found = next((d for d in profile.data_dirs() if d.is_dir()), None)
        folder, why = (found, "found") if found else (profile.data_dirs()[0], "default")
    exes = [exe_setting] if exe_setting else profile.executables()
    return Resolved(profile, folder, folder / "Config" / "Dolphin.ini", folder.is_dir(), exes[0] if exes else "", why)


# RomM platform slug -> setting group (the key under `emulators.` in the settings)
PLATFORM_GROUPS = {"ps2": "ps2", **{slug: "retroarch" for slug in RETROARCH_SYSTEMS}, **{slug: "dolphin" for slug in DOLPHIN_SLUGS}}


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


# --- the BIOS folder ------------------------------------------------------------------------------------

BIOS_MIN_BYTES = 4 * 1024 * 1024      # the emulator only considers files from 4 to 8 MiB (pcsx2/ps2/BiosTools.cpp)
BIOS_MAX_BYTES = 8 * 1024 * 1024
BIOS_EXTRAS = (".rom1", ".rom2", ".erom", ".nvm", ".mec")      # optional parts that sit beside a BIOS image
ROMDIR_SCAN_BYTES = 512 * 1024        # where the emulator looks for the image's ROM directory


def bios_dir(data_dir: Path) -> Path:
    """Where the emulator looks for BIOS images: [Folders] Bios of PCSX2.ini, `bios` under the data folder by default.
    A relative setting is relative to the data folder (the Armada's ARMSX2 ships `../../Downloads`)."""
    raw = read_ini(Path(data_dir) / "inis" / "PCSX2.ini").get("Folders", {}).get("Bios", "bios") or "bios"
    return Path(os.path.realpath(raw if os.path.isabs(raw) else Path(data_dir) / raw))


_image_cache: dict[tuple, bool] = {}


def is_bios_image(path: Path) -> bool:
    """A PS2 BIOS image: 4 to 8 MiB, with the ROM directory the emulator reads, an entry named RESET on a 16-byte
    boundary in the first 512 KiB. A folder like Downloads holds many files of that size; only this tells them apart."""
    try:
        st = Path(path).stat()
    except OSError:
        return False
    if not BIOS_MIN_BYTES <= st.st_size <= BIOS_MAX_BYTES:
        return False
    key = (str(path), st.st_size, st.st_mtime_ns)
    hit = _image_cache.get(key)
    if hit is not None:
        return hit
    found = False
    try:
        with open(path, "rb") as f:
            head = f.read(ROMDIR_SCAN_BYTES)
        pos = head.find(b"RESET")
        while pos != -1 and not found:
            found = pos % 16 == 0
            pos = head.find(b"RESET", pos + 1)
    except OSError:
        return False
    if len(_image_cache) > 256:
        _image_cache.clear()
    _image_cache[key] = found
    return found


def list_bios(directory: Path) -> dict:
    """What the BIOS folder holds that matters: {files: [{name, size, kind}], others: n}. kind: bios (a BIOS image) or
    extra (a part that goes with one). `others` counts everything else in the folder, which may be the Downloads folder."""
    files: list[dict] = []
    others = 0
    try:
        entries = sorted(Path(directory).iterdir())
    except OSError:
        return {"files": files, "others": others}
    for p in entries:
        if p.name.startswith("."):      # our own half-finished downloads, the file manager's leftovers
            continue
        try:
            if not p.is_file():
                continue
            size = p.stat().st_size
        except OSError:
            continue
        if p.name.lower().endswith(BIOS_EXTRAS):
            files.append({"name": p.name, "size": size, "kind": "extra"})
        elif is_bios_image(p):
            files.append({"name": p.name, "size": size, "kind": "bios"})
        else:
            others += 1
    return {"files": files, "others": others}


# --- running programs ------------------------------------------------------------------------------


@dataclass
class Instance:
    pid: int
    comm: str
    exe: str
    cmdline: list
    started: float                # seconds since boot, as /proc/<pid>/stat has it (0 if unknown)
    proc_root: str = "/proc"

    def open_files(self) -> list[str]:
        """Paths of the files the process holds open. The emulator keeps the disc image open while a game runs, which says
        which game it is even when it was started from its own menu, with nothing on the command line."""
        base = os.path.join(self.proc_root, str(self.pid), "fd")
        try:
            names = os.listdir(base)
        except OSError:
            return []
        out: list[str] = []
        for n in sorted(names, key=lambda x: (int(x) if x.isdigit() else -1, x)):
            try:
                target = os.readlink(os.path.join(base, n))
            except OSError:
                continue
            if target.startswith("/") and not target.startswith(("/dev/", "/proc/", "/sys/")) and not target.endswith(" (deleted)"):
                out.append(target)
        return out

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
        found.append(Instance(int(e), comm, exe, cmd, started, proc_root))
    return found


def is_running(profile: Profile, proc_root: str = "/proc") -> bool:
    return bool(find_instances(profile.process_names, proc_root))


def all_ps2_process_names() -> tuple:
    return tuple(sorted({n for p in PROFILES.values() if p.family == "pcsx2" for n in p.process_names}))


def process_names_for(group: str | None) -> tuple:
    """The program names of the emulators that play a setting group's games (all of them when the group is unknown)."""
    if group == "ps2":
        return all_ps2_process_names()
    if group in ("retroarch", "dolphin"):
        return PROFILES[group].process_names
    return all_process_names()


def all_process_names() -> tuple:
    return tuple(sorted({n for p in PROFILES.values() for n in p.process_names}))


def serial_from_path(rom: str | None) -> str | None:
    """A PS2 serial in a file name, e.g. 'SLUS-20152' or 'SLUS_201.52', when the name has one."""
    if not rom:
        return None
    m = re.search(r"\b([A-Z]{4})[-_ ]?(\d{3})[._]?(\d{2})\b", os.path.basename(rom).upper())
    return f"{m.group(1)}-{m.group(2)}{m.group(3)}" if m else None
