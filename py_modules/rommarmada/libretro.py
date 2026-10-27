# SPDX-License-Identifier: MIT
"""RetroArch saves: one `<game>.srm` per game (and `<game>.rtc` for a cart with a clock) in RetroArch's save folder.

This is the default libretro row of argosy-sigil (docs/platforms/gb.md, "Save layouts"), which fits every core that keeps
its battery save under the game's own name. The plugin follows the row directly instead of asking sigil's collect and
restore to do it, because those cover only the systems sigil can identify a game for, and a Game Boy Advance or Super
Nintendo game is not one of them. What goes to RomM is what Argosy sends for the same row: the `.srm` as it is, or a flat
zip of the `.srm` and `.rtc` when both exist (named `<game>.srm.zip`). RomM hashes the first by its bytes and the second by
its entries, as for every other save, so the same save is the same save however it was zipped.

Where the folder is: RetroArch's `savefile_directory`, or next to the game when it says nothing. It can sort saves into a
folder per core or per game folder; that is read from `retroarch.cfg`, and a save that already exists is found wherever it is.
"""
from __future__ import annotations

import hashlib
import io
import os
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import emulators, paths
from .savefolders import ArchiveError, hash_entries, romm_content_hash

MAX_BYTES = 64 * 1024 * 1024        # a battery save is a few KB to a few MB; nothing bigger is a save
EXTENSIONS = (".srm", ".rtc")


# -- retroarch.cfg ---------------------------------------------------------------------------------------


def read_cfg(path: Path) -> dict[str, str]:
    """`key = "value"` lines, without the quotes."""
    out: dict[str, str] = {}
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in "#;" or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1]
        out[key.strip()] = value
    return out


def _is_true(value: str | None) -> bool:
    return (value or "").strip().lower() == "true"


def _cfg_dir(value: str | None, data_dir: Path) -> Path | None:
    """A folder setting of retroarch.cfg: empty or `default` means none; `:/x` is under the config folder."""
    v = (value or "").strip()
    if not v or v.lower() == "default":
        return None
    if v.startswith(":"):
        return data_dir / v[1:].lstrip("/\\")
    if v == "~" or v.startswith("~/"):
        return paths.user_home() / v[2:]
    p = Path(v)
    return p if p.is_absolute() else data_dir / v


@dataclass
class Config:
    data_dir: Path
    cfg_exists: bool
    save_dir: Path | None          # None: next to the game
    sort_by_core: bool
    sort_by_content: bool
    why: str                       # where the data folder came from: setting, found, default
    state_dir: Path | None = None  # RetroArch's save states: None next to the game
    state_sort_by_core: bool = False
    state_sort_by_content: bool = False


def config_of(data_dir: Path, save_dir_setting: str = "", why: str = "default") -> Config:
    data_dir = Path(data_dir)
    cfg_path = data_dir / "retroarch.cfg"
    cfg = read_cfg(cfg_path)
    in_content = _is_true(cfg.get("savefiles_in_content_dir"))
    save_dir = Path(save_dir_setting) if save_dir_setting else (None if in_content else _cfg_dir(cfg.get("savefile_directory"), data_dir))
    state_dir = None if _is_true(cfg.get("savestates_in_content_dir")) else _cfg_dir(cfg.get("savestate_directory"), data_dir)
    return Config(data_dir, cfg_path.is_file(), save_dir, _is_true(cfg.get("sort_savefiles_enable")),
                  _is_true(cfg.get("sort_savefiles_by_content_enable")), why, state_dir,
                  _is_true(cfg.get("sort_savestates_enable")), _is_true(cfg.get("sort_savestates_by_content_enable")))


@dataclass
class Resolved:
    profile: emulators.Profile
    config: Config
    executable: str

    @property
    def data_dir(self) -> Path:
        return self.config.data_dir

    @property
    def data_dir_exists(self) -> bool:
        return self.config.data_dir.is_dir()


def resolve(data_dir_setting: str = "", save_dir_setting: str = "", exe_setting: str = "") -> Resolved:
    """RetroArch's folder: the setting wins, then the first one that exists, then the usual place."""
    profile = emulators.PROFILES["retroarch"]
    if data_dir_setting:
        folder, why = Path(data_dir_setting), "setting"
    else:
        found = next((d for d in profile.data_dirs() if d.is_dir()), None)
        folder, why = (found, "found") if found else (profile.data_dirs()[0], "default")
    exes = [exe_setting] if exe_setting else profile.executables()
    return Resolved(profile, config_of(folder, save_dir_setting, why), exes[0] if exes else "")


# -- where a game's save is ----------------------------------------------------------------------------------


def _subdirs(d: Path) -> list[Path]:
    try:
        return sorted(p for p in d.iterdir() if p.is_dir() and not p.is_symlink() and not p.name.startswith("."))
    except OSError:
        return []


def _names(rom_path: str) -> list[str]:
    return [Path(rom_path).stem + ext for ext in EXTENSIONS]


def save_root(cfg: Config, rom_path: str) -> tuple[Path | None, str]:
    """The folder that holds (or will hold) this game's `.srm`, and a note. None when RetroArch sorts saves by core and
    this game has none yet, because the core's folder name is not known until the game has been played once."""
    base = cfg.save_dir if cfg.save_dir is not None else Path(rom_path).parent
    sorted_saves = cfg.save_dir is not None and (cfg.sort_by_core or cfg.sort_by_content)
    if not sorted_saves:
        return base, ""
    candidates = [base]
    for sub in _subdirs(base):
        candidates.append(sub)
        if cfg.sort_by_core and cfg.sort_by_content:
            candidates += _subdirs(sub)
    names = _names(rom_path)
    holding = [d for d in candidates if any((d / n).is_file() for n in names)]
    if holding:
        def newest(d: Path) -> float:
            return max((d / n).stat().st_mtime for n in names if (d / n).is_file())
        return max(holding, key=newest), ""
    if cfg.sort_by_content and not cfg.sort_by_core:
        return base / Path(rom_path).parent.name, ""
    return None, ("RetroArch keeps saves in a folder per core. Play this game once, then its save can be synced "
                  "(or turn off 'Sort Saves into Folders by Core Name' in RetroArch)")


# -- the unit RomM keeps -----------------------------------------------------------------------------------------


def _members(root: Path, rom_path: str) -> list[Path]:
    """The save files this game has: the `.srm` (nothing without it) and the `.rtc` beside it."""
    stem = Path(rom_path).stem
    srm = root / f"{stem}.srm"
    if not srm.is_file():
        return []
    rtc = root / f"{stem}.rtc"
    return [srm] + ([rtc] if rtc.is_file() else [])


def files_of(root: Path, rom_path: str) -> list[Path]:
    """The files a restore would replace: for the backup before it."""
    return [root / n for n in _names(rom_path) if (root / n).is_file()]


def collect(root: Path, rom_path: str, work: Path, last: dict | None) -> dict:
    """What this device holds for the game, as a unit in `work`. The keys are the ones the sync engine reads."""
    stem = Path(rom_path).stem
    got = {"ok": True, "format": "file", "has_unit": False, "unit_path": "", "state_tmp": "", "content_hash": "",
           "changed": False, "restore_again": False, "artifact": f"{stem}.srm", "mtime": 0.0, "identity_hash": ""}
    members = _members(root, rom_path)
    if not members:
        return got
    for m in members:
        if m.stat().st_size > MAX_BYTES:
            raise ArchiveError(f"{m.name} is larger than a battery save can be")
    work.mkdir(parents=True, exist_ok=True)
    if len(members) == 1:
        data = members[0].read_bytes()
        unit = work / "unit.srm"
        unit.write_bytes(data)
        digest = hashlib.md5(data).hexdigest()
        identity = digest
    else:
        unit = work / "unit.zip"
        entries: list[tuple[str, str]] = []
        with zipfile.ZipFile(unit, "w", zipfile.ZIP_DEFLATED) as zf:
            for m in members:
                data = m.read_bytes()
                zf.writestr(zipfile.ZipInfo(m.name, date_time=time.localtime(max(m.stat().st_mtime, 315532800))[:6]), data,
                            compress_type=zipfile.ZIP_DEFLATED)
                entries.append((m.name, hashlib.md5(data).hexdigest()))
        digest = hash_entries(entries)
        identity = hashlib.md5(members[0].read_bytes()).hexdigest()      # the clock alone is not a new save
        got["artifact"] = f"{stem}.srm.zip"
    got.update(has_unit=True, unit_path=str(unit), content_hash=digest, identity_hash=identity,
               mtime=max(m.stat().st_mtime for m in members),
               changed=not (last and last.get("local_hash") == digest))
    return got


def restore(data: bytes, file_name: str, root: Path, rom_path: str) -> dict:
    """Writes a downloaded save under this game's name. A flat zip gives its `.srm` and `.rtc`; any other file is the `.srm`.
    Raises ArchiveError for one that holds no battery save. Everything is checked before anything is written, and the old
    files are put back if writing fails."""
    stem = Path(rom_path).stem
    chosen: dict[str, bytes] = {}
    if zipfile.is_zipfile(io.BytesIO(data)):
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            held: list[str] = []
            for info in zf.infolist():
                if info.is_dir():
                    continue
                name = info.filename.replace("\\", "/")
                parts = PurePosixPath(name).parts
                if name.startswith("/") or ".." in parts or not parts:
                    raise ArchiveError(f"The archive holds an unsafe path ({info.filename})")
                held.append(parts[-1])
                ext = PurePosixPath(parts[-1]).suffix.lower()
                if len(parts) == 1 and ext in EXTENSIONS and ext not in chosen:
                    if info.file_size > MAX_BYTES:
                        raise ArchiveError(f"{parts[-1]} is larger than a battery save can be")
                    chosen[ext] = zf.read(info)
            if ".srm" not in chosen:
                seen = ", ".join(held[:5]) + (" …" if len(held) > 5 else "")
                raise ArchiveError("RomM's save is not a RetroArch save" + (f" (it holds {seen})" if held else " (it is empty)"))
    else:
        if len(data) > MAX_BYTES:
            raise ArchiveError("RomM's save is larger than a battery save can be")
        if not data:
            raise ArchiveError("RomM's save is empty")
        chosen[".rtc" if (file_name or "").lower().endswith(".rtc") else ".srm"] = data
    root.mkdir(parents=True, exist_ok=True)
    old: dict[Path, bytes | None] = {}
    written: list[Path] = []
    try:
        for ext, blob in chosen.items():
            target = root / f"{stem}{ext}"
            old[target] = target.read_bytes() if target.is_file() else None
            tmp = root / f".{target.name}.romm-armada"
            tmp.write_bytes(blob)
            os.chmod(tmp, 0o644)
            os.replace(tmp, target)
            written.append(target)
    except OSError:
        for target in written:
            before = old.get(target)
            try:
                if before is None:
                    target.unlink(missing_ok=True)
                else:
                    target.write_bytes(before)
            except OSError:
                pass
        raise
    return {"content_hash": romm_content_hash(data), "identity_hash": "", "written": [p.name for p in written]}
