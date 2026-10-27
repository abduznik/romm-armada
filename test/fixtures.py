# SPDX-License-Identifier: MIT
"""Fixtures shared by the tests: a minimal PS2 disc image, save zips, and a fake ARMSX2 data folder."""
from __future__ import annotations

import io
import os
import struct
import zipfile

SECTOR = 2048


def _dir_record(name: bytes, lba: int, length: int, is_dir: bool = False) -> bytes:
    rec_len = 33 + len(name)
    if rec_len % 2:
        rec_len += 1
    rec = bytearray(rec_len)
    rec[0] = rec_len
    struct.pack_into("<I", rec, 2, lba)
    struct.pack_into(">I", rec, 6, lba)
    struct.pack_into("<I", rec, 10, length)
    struct.pack_into(">I", rec, 14, length)
    rec[25] = 2 if is_dir else 0
    rec[32] = len(name)
    rec[33:33 + len(name)] = name
    return bytes(rec)


def make_ps2_iso(serial: str = "SLUS-20152", extra_bytes: int = 0) -> bytes:
    """A tiny ISO9660 image whose SYSTEM.CNF names `serial`, which is all sigil reads for PS2."""
    boot = serial.replace("-", "_")
    boot = boot[:4] + "_" + boot[5:8] + "." + boot[8:]  # SLUS-20152 -> SLUS_201.52
    cnf = f"BOOT2 = cdrom0:\\{boot};1\r\nVER = 1.00\r\nVMODE = NTSC\r\n".encode()
    exe = b"\x7fELF" + b"\0" * 60
    root_lba, cnf_lba, exe_lba = 18, 19, 20
    root = bytearray(SECTOR)
    pos = 0
    for rec in (
        _dir_record(b"\x00", root_lba, SECTOR, True),
        _dir_record(b"\x01", root_lba, SECTOR, True),
        _dir_record(b"SYSTEM.CNF;1", cnf_lba, len(cnf)),
        _dir_record((boot + ";1").encode(), exe_lba, len(exe)),
    ):
        root[pos:pos + len(rec)] = rec
        pos += len(rec)
    pvd = bytearray(SECTOR)
    pvd[0] = 1
    pvd[1:6] = b"CD001"
    pvd[6] = 1
    pvd[40:72] = b"TESTDISC".ljust(32)
    struct.pack_into("<I", pvd, 80, 21 + extra_bytes // SECTOR)
    struct.pack_into(">I", pvd, 84, 21 + extra_bytes // SECTOR)
    pvd[156:156 + 34] = _dir_record(b"\x00", root_lba, SECTOR, True)[:34]
    term = bytearray(SECTOR)
    term[0] = 255
    term[1:6] = b"CD001"
    term[6] = 1
    out = bytearray(SECTOR * 16)
    out += pvd + term + bytes(SECTOR)  # 16, 17, 17+1 (unused)
    out[SECTOR * 18:SECTOR * 19] = root
    out += cnf.ljust(SECTOR, b"\0") + exe.ljust(SECTOR, b"\0")
    out += b"\0" * extra_bytes
    return bytes(out)


def make_save_zip(folder: str = "BASLUS-20152", files: dict[str, bytes] | None = None) -> bytes:
    """A zip of one PCSX2 save folder: the shape sigil accepts from clients that predate it."""
    files = files or {
        "icon.sys": b"PS2D" + b"\0" * 60 + b"title",
        "save.dat": bytes(range(256)) * 8,
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for name, data in files.items():
            z.writestr(f"{folder}/{name}", data)
    return buf.getvalue()


def write(path: str, data: bytes) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return path


def make_gc_iso(game_id: str = "GZLE") -> bytes:
    """The start of a GameCube disc: its game id and the console magic sigil reads."""
    iso = bytearray(1 << 20)
    iso[0:4] = game_id.encode()
    iso[0x1C:0x20] = bytes.fromhex("C2339F3D")
    iso[0x20:0x29] = b"Test Game"
    return bytes(iso)


def make_gci(game_id: str = "GZLE", name: str = "TestSave", seed: int = 7) -> bytes:
    """A one-block GameCube save file (.gci): the 0x40-byte entry and an 8 KiB block."""
    import struct
    hdr = bytearray(0x40)
    hdr[0:4] = game_id.encode()
    hdr[4:6] = b"01"
    hdr[6] = 0xFF
    hdr[8:8 + len(name)] = name.encode()
    struct.pack_into(">H", hdr, 0x38, 1)
    struct.pack_into(">H", hdr, 0x3A, 0xFFFF)
    return bytes(hdr) + bytes((i * seed) & 255 for i in range(0x2000))
