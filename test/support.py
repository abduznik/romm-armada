# SPDX-License-Identifier: MIT
"""Shared test setup: an isolated home, the mock server, and a ready-made PS2 game."""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py_modules"))
sys.path.insert(0, str(ROOT / "test"))

import fixtures  # noqa: E402
import mock_romm  # noqa: E402

BIN = ROOT / "bin"


def helper_arch() -> str:
    forced = os.environ.get("ROMM_ARMADA_HELPER_ARCH")
    return forced or "x86_64"


class Env(unittest.TestCase):
    """Each test gets its own HOME, plugin folders, mock server and settings."""

    maxDiff = None

    def setUp(self):
        from rommarmada import config, connect, library, paths, sigil  # noqa: F401

        self.tmp = Path(tempfile.mkdtemp(prefix="rommarmada-test-"))
        self._env = {k: os.environ.get(k) for k in ("HOME", "XDG_CONFIG_HOME", "ROMM_ARMADA_HOME", "DECKY_USER_HOME",
                                                    "ROMM_ARMADA_HELPER_ARCH", "DECKY_PLUGIN_DIR")}
        self.home = self.tmp / "home"
        self.home.mkdir()
        os.environ["HOME"] = str(self.home)
        os.environ["DECKY_USER_HOME"] = str(self.home)
        os.environ["XDG_CONFIG_HOME"] = str(self.home / ".config")
        os.environ["ROMM_ARMADA_HOME"] = str(self.tmp / "plugin")
        os.environ["ROMM_ARMADA_HELPER_ARCH"] = helper_arch()
        os.environ.pop("DECKY_PLUGIN_DIR", None)
        self.mock = mock_romm.MockRomm()
        self.url = self.mock.start()
        self.settings = config.Settings()
        self.settings.load()
        self.settings.update({"library": {"root": str(self.home / "ROMs")}, "advanced": {"request_timeout": 10}})
        self.manifest = library.Manifest()
        self.events: list[tuple[str, dict]] = []

    def tearDown(self):
        self.mock.stop()
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def emit(self, event, payload):
        self.events.append((event, payload))

    # -- builders -----------------------------------------------------------------------------------------

    def sign_in(self, scopes=None):
        """Settings that look like a finished pairing against the mock."""
        dev = "dev-" + os.urandom(3).hex()
        self.mock.devices[dev] = {"id": dev, "name": "Test handheld", "hostname": "armada", "platform": "linux",
                                  "sync_enabled": True, "created_at": mock_romm.iso(self.mock.now())}
        tok = self.mock.make_token(scopes)
        self.settings.update({"server": {"url": self.url}, "auth": {"token": tok, "device_id": dev, "username": "tester"}})
        return dev

    def client(self):
        from rommarmada.http import HttpClient
        from rommarmada.romm import RommClient
        return RommClient(HttpClient(self.url, token=self.settings.get("auth.token"), timeout=10))

    def ps2_platform(self):
        return self.mock.add_platform("ps2", "PlayStation 2")

    def install_ps2_game(self, name="Test Game (USA)", serial="SLUS-20152", rom_id=None):
        """A PS2 'game' on disk with a manifest entry, as the download manager would leave it."""
        from rommarmada import library

        pid = next(iter(self.mock.platforms), None) or self.ps2_platform()
        rid = rom_id or self.mock.add_rom(pid, name, [(f"{name}.iso", fixtures.make_ps2_iso(serial))], title_id=serial)
        rom = self.mock.roms[rid]
        layout = library.Layout(self.settings.get("library.root"), "esde", {})
        files = []
        for f in rom["files"]:
            dest = layout.destination(rom, f)
            fixtures.write(str(dest), self.mock.file_data[f["id"]])
            files.append((f, dest))
        entry = library.make_entry(rom, layout, files, files[0][1])
        self.manifest.put(entry)
        return entry

    def savebridge(self):
        from rommarmada.sigil import Savebridge
        return Savebridge(BIN)

    def card_dir(self, name="ARMSX2"):
        return self.home / ".config" / name


class Device:
    """A second handheld sharing the mock server: its own home, settings, manifest and sync engine."""

    ENV = ("HOME", "DECKY_USER_HOME", "XDG_CONFIG_HOME", "ROMM_ARMADA_HOME")

    def __init__(self, env: "Env", name: str) -> None:
        from contextlib import contextmanager  # noqa: F401
        self.env, self.name = env, name
        self.home = env.tmp / f"home-{name}"
        self.home.mkdir()
        self.plugin = env.tmp / f"plugin-{name}"
        with self.active():
            from rommarmada import config, library
            self.settings = config.Settings()
            self.settings.load()
            self.settings.update({"library": {"root": str(self.home / "ROMs")}, "advanced": {"request_timeout": 10},
                                  "auth": {"device_name": name}})
            self.manifest = library.Manifest()
        dev = f"dev-{name}"
        env.mock.devices[dev] = {"id": dev, "name": name, "hostname": name, "platform": "linux", "sync_enabled": True,
                                 "created_at": mock_romm.iso(env.mock.now())}
        self.settings.update({"server": {"url": env.url},
                              "auth": {"token": env.mock.make_token(), "device_id": dev, "username": "tester"}})
        self.device_id = dev
        self.sync = None

    def active(self):
        from contextlib import contextmanager

        @contextmanager
        def cm():
            saved = {k: os.environ.get(k) for k in self.ENV}
            os.environ.update({"HOME": str(self.home), "DECKY_USER_HOME": str(self.home),
                               "XDG_CONFIG_HOME": str(self.home / ".config"), "ROMM_ARMADA_HOME": str(self.plugin)})
            try:
                yield self
            finally:
                for k, v in saved.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v
        return cm()

    def engine(self, proc_root="/proc"):
        from rommarmada.http import HttpClient
        from rommarmada.romm import RommClient
        from rommarmada.saves import SaveSync
        from rommarmada.sigil import Savebridge

        def client():
            return RommClient(HttpClient(self.env.url, token=self.settings.get("auth.token"), timeout=10))
        self.sync = SaveSync(self.settings, self.manifest, client, Savebridge(BIN), self.env.emit, proc_root=proc_root)
        return self.sync

    def card(self, name="Mcd001.ps2") -> Path:
        return self.home / ".config" / "ARMSX2" / "memcards" / name

    def install(self, rom_id: int) -> dict:
        """Put the ROM from the mock on this device, as a finished download would."""
        from rommarmada import library
        rom = self.env.mock.roms[rom_id]
        lay = library.Layout(self.settings.get("library.root"), "esde", {})
        files = []
        for f in rom["files"]:
            dest = lay.destination(rom, f)
            fixtures.write(str(dest), self.env.mock.file_data[f["id"]])
            files.append((f, dest))
        entry = library.make_entry(rom, lay, files, files[0][1])
        self.manifest.put(entry)
        return entry

    def play(self, content: bytes, serial_folder="BASLUS-20152", file="save.dat") -> None:
        """What the emulator does when the player saves: the game's save folder changes on the folder card."""
        from rommarmada.sigil import Savebridge
        if not (self.card() / "_pcsx2_superblock").exists() and not self.card().is_file():
            Savebridge(BIN).init_folder_card(str(self.home / ".config" / "ARMSX2"), "Mcd001.ps2")   # a card the emulator formatted
        folder = self.card() / serial_folder
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "icon.sys").write_bytes(b"PS2D" + b"\0" * 60 + b"title")
        (folder / file).write_bytes(content)
        time.sleep(0.02)

    def save_bytes(self, serial_folder="BASLUS-20152", file="save.dat") -> bytes:
        return (self.card() / serial_folder / file).read_bytes()
