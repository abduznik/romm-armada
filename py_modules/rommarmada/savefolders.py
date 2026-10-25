# SPDX-License-Identifier: MIT
"""PS2 saves as a game's save folders in a zip: what Argosy and Freegosy keep on RomM before 5.5.

A PCSX2 folder memory card is a directory (`Mcd001.ps2/`) with `_pcsx2_superblock` and one directory per save, named
`<region prefix><serial><suffix>` (`BASLUS-21269BULLY000`). The upload is a zip holding only the folders of the game
being synced, each at the root of the archive. A game's save is a few hundred KB this way, not an 8 MB card image, and
Argosy, Freegosy and this plugin read each other's uploads.

RomM 5.5 and newer keep sigil's per-game card image instead (see sigil.py); the sync engine picks by what the server
says and restores either shape.

RomM gives a zip save a content hash made of its entries, not of the zip's bytes: the MD5 of the sorted
`name:md5(entry)` lines (handler/filesystem/assets_handler.py, `hash_zip_contents`). The hash computed here is the
same, so a save that did not change hashes the same on every device however it was zipped.
"""
from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
import stat
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

SUPERBLOCK = "_pcsx2_superblock"
IMAGE_MIN_BYTES = 4 * 1024 * 1024        # a memory card image is 8 MB (older cards: 4)
MAX_BYTES = 64 * 1024 * 1024          # a memory card holds 8 MB; anything bigger is not a PS2 save
CHUNK = 64 * 1024
HASH_ENTRY_LIMIT = 512 * 1024 * 1024      # RomM does not unpack a zip entry bigger than this to hash it


class ArchiveError(Exception):
    """An archive that cannot be used, or a card that cannot take it. The text is for the user."""


class NoFiles(ArchiveError):
    """The game's save folders exist but hold no files: there is nothing to upload."""


# -- which folders are a game's ----------------------------------------------------------------------------


_REGION_PREFIXED = re.compile(r"^B[AEI][A-Z]{4}")
_BARE_SERIAL = re.compile(r"^([A-Z]{4})(\d+.*)$")


def _norm(value: str) -> str:
    return value.replace("-", "").replace("_", "").upper()


def _stem(save_id: str) -> str:
    """sigil's save_id (`BASLUS-21269`) without punctuation. A bare serial (`SLUS-21269`) gets its region prefix:
    BE for PAL, BI for Japan and Korea, BA otherwise."""
    cleaned = _norm(save_id)
    if _REGION_PREFIXED.match(cleaned):
        return cleaned
    m = _BARE_SERIAL.match(cleaned)
    if not m:
        return cleaned
    code, rest = m.groups()
    return ("BE" if code[2] == "E" else "BI" if code[2] in "PJK" else "BA") + code + rest


def _without_region(value: str) -> str:
    return value[2:] if _REGION_PREFIXED.match(value) else value


def folder_matches(folder_name: str, save_id: str) -> bool:
    """Whether a card folder is one of the game's: its name starts with the game's stem (`BASLUS21269BULLY000`,
    `BASLUS-20642SYS` and `BASLUS-20642RD0` are one game's). A wrong region letter still finds it."""
    if not save_id:
        return False
    stem, name = _stem(save_id), _norm(folder_name)
    return name.startswith(stem) or _without_region(name).startswith(_without_region(stem))


def find_folders(cards: list[Path], save_id: str) -> list[Path]:
    """The game's save folders on the given folder cards (in slot order; a name on two cards is the first card's)."""
    seen: dict[str, Path] = {}
    for card in cards:
        try:
            children = sorted(card.iterdir())
        except OSError:
            continue
        for d in children:
            if d.name.startswith(".") or d.is_symlink() or not d.is_dir():
                continue
            if folder_matches(d.name, save_id) and d.name not in seen:
                seen[d.name] = d
    return list(seen.values())


# -- RomM's content hash ---------------------------------------------------------------------------------------


def hash_entries(entries: list[tuple[str, str]]) -> str:
    """RomM's hash of a zip: MD5 over `name:md5` lines, sorted by name, joined with newlines."""
    combined = "\n".join(f"{name}:{digest}" for name, digest in sorted(entries))
    return hashlib.md5(combined.encode()).hexdigest()


def romm_content_hash(data: bytes) -> str:
    """The `content_hash` RomM gives a save with these bytes: by entries for a zip, the MD5 of the bytes otherwise."""
    bio = io.BytesIO(data)
    if zipfile.is_zipfile(bio):
        try:
            with zipfile.ZipFile(bio) as zf:
                if any(i.file_size > HASH_ENTRY_LIMIT for i in zf.infolist()):
                    return hashlib.md5(data).hexdigest()      # as RomM does: it refuses to unpack such an entry to hash it
                entries = []
                for name in zf.namelist():
                    if not name.endswith("/"):
                        h = hashlib.md5()
                        with zf.open(name) as f:
                            for chunk in iter(lambda: f.read(CHUNK), b""):
                                h.update(chunk)
                        entries.append((name, h.hexdigest()))
                return hash_entries(entries)
        except (zipfile.BadZipFile, OSError, RuntimeError):
            pass
    return hashlib.md5(data).hexdigest()


# -- building the upload ----------------------------------------------------------------------------------


@dataclass
class Built:
    content_hash: str
    size: int                    # the zip, as uploaded
    folders: list[str]
    files: int
    data_bytes: int              # what the folders hold, uncompressed
    mtime: float                 # when the newest file was written


def _date_time(mtime: float) -> tuple:
    t = time.localtime(max(mtime, 315532800))      # zip dates start in 1980
    return t[:6]


def build(folders: list[Path], out: Path) -> Built:
    """Zips the folders, each at the root of the archive (`BASLUS-21269BULLY000/icon.sys`), and returns the hash RomM will
    give the upload. Raises ArchiveError when they hold no files or more than a memory card can."""
    entries: list[tuple[str, str]] = []
    total = files = 0
    newest = 0.0
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for folder in sorted(folders, key=lambda p: p.name):
            top = zipfile.ZipInfo(folder.name + "/", date_time=_date_time(folder.stat().st_mtime))
            top.external_attr = (stat.S_IFDIR | 0o755) << 16 | 0x10
            zf.writestr(top, b"")
            for path in sorted(p for p in folder.rglob("*") if not p.is_symlink()):
                rel = path.relative_to(folder).as_posix()
                if path.is_dir():
                    info = zipfile.ZipInfo(f"{folder.name}/{rel}/", date_time=_date_time(path.stat().st_mtime))
                    info.external_attr = (stat.S_IFDIR | 0o755) << 16 | 0x10
                    zf.writestr(info, b"")
                    continue
                if not path.is_file():
                    continue
                st = path.stat()
                total += st.st_size
                if total > MAX_BYTES:
                    raise ArchiveError("The save folders are larger than a memory card can hold")
                data = path.read_bytes()
                info = zipfile.ZipInfo(f"{folder.name}/{rel}", date_time=_date_time(st.st_mtime))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (stat.S_IFREG | 0o644) << 16
                zf.writestr(info, data)
                entries.append((info.filename, hashlib.md5(data).hexdigest()))
                files += 1
                newest = max(newest, st.st_mtime)
    if not files:
        out.unlink(missing_ok=True)
        raise NoFiles("The game's save folders hold no files")
    return Built(hash_entries(entries), out.stat().st_size, [f.name for f in folders], files, total, newest)


def describe(path: Path) -> dict:
    """What a zip of save folders is, for the screen: its size, the folders inside, and the data they hold."""
    info: dict = {"kind": "folders", "size": 0, "contents": [], "bytes": 0}
    try:
        info["size"] = Path(path).stat().st_size
        with zipfile.ZipFile(path) as zf:
            tops: list[str] = []
            for i in zf.infolist():
                parts = i.filename.replace("\\", "/").split("/")
                if len(parts) > 1 and parts[0] not in tops and parts[0] != "__MACOSX":
                    tops.append(parts[0])
                if not i.is_dir():
                    info["bytes"] += i.file_size
            info["contents"] = tops
    except (OSError, zipfile.BadZipFile):
        pass
    return info


# -- restoring a download ----------------------------------------------------------------------------------


@dataclass
class Plan:
    shape: str                                           # folders (rooted at the game folders), card (rooted at a card) or image
    folders: dict[str, list[tuple[str, zipfile.ZipInfo]]] = field(default_factory=dict)   # folder -> (path inside, entry)
    images: list[zipfile.ZipInfo] = field(default_factory=list)    # memory card images (.ps2 files) the archive carries
    ignored: int = 0                                     # entries that are not this game's (other games on a whole card)


def _clean(name: str) -> str:
    return name.replace("\\", "/")


def _safe_parts(name: str) -> tuple[str, ...]:
    """The parts of an archive path, or ArchiveError for one that could leave the folder it is unpacked into."""
    n = _clean(name)
    p = PurePosixPath(n)
    if n.startswith("/") or "\x00" in n or p.is_absolute() or re.match(r"^[A-Za-z]:", n):
        raise ArchiveError(f"The archive holds an unsafe path ({name})")
    parts = tuple(x for x in p.parts if x not in ("", "."))
    if ".." in parts:
        raise ArchiveError(f"The archive holds an unsafe path ({name})")
    return parts


def plan(zf: zipfile.ZipFile, save_id: str) -> Plan:
    """What a downloaded zip holds for this game. Understands the shapes in use: the game's folders at the root (Argosy,
    Freegosy, this plugin) and a whole folder card (the folder with `_pcsx2_superblock`, as older clients uploaded),
    in which only this game's folders count. Raises ArchiveError for anything unsafe or with nothing of this game."""
    infos = [i for i in zf.infolist() if not i.is_dir()]
    parsed: list[tuple[tuple[str, ...], zipfile.ZipInfo]] = []
    for i in infos:
        mode = i.external_attr >> 16
        if mode and stat.S_ISLNK(mode):
            raise ArchiveError(f"The archive holds a link ({i.filename})")
        parts = _safe_parts(i.filename)
        if parts and parts[0] != "__MACOSX":
            parsed.append((parts, i))
    # a folder card is the directory with the superblock, or one named like a card (Mcd001.ps2/)
    card_roots = {parts[0] for parts, _ in parsed if len(parts) >= 2 and (
        (len(parts) == 2 and parts[1] == SUPERBLOCK) or parts[0].lower().endswith(".ps2"))}
    out = Plan("card" if card_roots else "folders")
    for parts, i in parsed:
        if parts[-1].lower().endswith(".ps2") and (len(parts) == 1 or (
                len(parts) == 2 and parts[0] not in card_roots and not folder_matches(parts[0], save_id))):
            if i.file_size >= IMAGE_MIN_BYTES:       # a whole memory card inside the zip (older Freegosy uploads)
                out.images.append(i)
            continue
        if parts[0] in card_roots:
            parts = parts[1:]
        if len(parts) < 2:          # loose files at the root: the superblock, freegosy_sync.txt, notes
            continue
        folder, rest = parts[0], "/".join(parts[1:])
        if not folder_matches(folder, save_id):
            out.ignored += 1
            continue
        out.folders.setdefault(folder, []).append((rest, i))
    wanted = sum(i.file_size for items in out.folders.values() for _, i in items)
    if wanted > MAX_BYTES:        # only what would be written counts: older bundles also carried save states of tens of MB
        raise ArchiveError("The save folders in the archive are larger than a memory card can hold")
    if not out.folders:
        if out.images:
            out.shape = "image"
            return out
        tops = sorted({(parts[1:] if parts[0] in card_roots else parts)[0] for parts, _ in parsed
                       if len(parts[1:] if parts[0] in card_roots else parts) >= 2})
        seen = ", ".join(tops[:6]) + (" …" if len(tops) > 6 else "")
        raise ArchiveError("RomM's save holds nothing for this game" + (f" (it has {seen})" if seen else ""))
    return out


def clean_zip(zf: zipfile.ZipFile, pl: Plan, out: Path) -> Path:
    """The planned folders as the plain zip sigil takes for a file memory card: files only, as `folder/file`, without PCSX2's
    own bookkeeping (`_pcsx2_index`), which a file card has no use for."""
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for folder, items in pl.folders.items():
            for rest, entry in items:
                if rest.startswith("_pcsx2_") or "/" in rest:
                    continue
                dst.writestr(f"{folder}/{rest}", zf.read(entry))
    return out


def _extract(zf: zipfile.ZipFile, entry: zipfile.ZipInfo, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with zf.open(entry) as src, open(dest, "wb") as dst:
        for chunk in iter(lambda: src.read(CHUNK), b""):
            written += len(chunk)
            if written > MAX_BYTES:
                raise ArchiveError("An entry in the archive is larger than a memory card can hold")
            dst.write(chunk)
    os.chmod(dest, 0o644)
    try:        # PCSX2 reads a save's dates from the files when there is no index to say otherwise
        ts = time.mktime(entry.date_time + (0, 0, -1))
        os.utime(dest, (ts, ts))
    except (OverflowError, ValueError, OSError):
        pass


def restore(zf: zipfile.ZipFile, pl: Plan, card: Path, save_id: str, scratch: Path) -> list[str]:
    """Puts the planned folders on `card` in place of the game's own: each is unpacked beside the card first and swapped in
    whole, and the game's folders the archive lacks are removed, so the card ends up holding what the archive holds.
    Nothing else on the card is touched. Returns the folder names written."""
    shutil.rmtree(scratch, ignore_errors=True)
    new, old = scratch / "new", scratch / "old"
    new.mkdir(parents=True)
    old.mkdir(parents=True)
    moved: list[str] = []         # folders whose old copy was put aside
    placed: list[str] = []        # folders whose new copy is in place
    try:
        for folder, items in pl.folders.items():
            for rest, entry in items:
                _extract(zf, entry, new / folder / rest)
        for folder in pl.folders:
            target = card / folder
            if target.exists() or target.is_symlink():
                shutil.move(str(target), str(old / folder))
                moved.append(folder)
            os.replace(new / folder, target)
            placed.append(folder)
        for stale in find_folders([card], save_id):
            if stale.name not in pl.folders:
                shutil.move(str(stale), str(old / stale.name))
                moved.append(stale.name)
    except (OSError, ArchiveError):
        for folder in placed:           # undo: the new copies out, the old ones back
            shutil.rmtree(card / folder, ignore_errors=True)
        for folder in moved:
            try:
                if (old / folder).exists():
                    shutil.move(str(old / folder), str(card / folder))
            except OSError:
                pass
        raise
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return placed
