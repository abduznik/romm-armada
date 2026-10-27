# SPDX-License-Identifier: MIT
"""Save states: the emulators' quick-save files, synced with RomM's states (one row per file name)."""
import os
import time
from datetime import timedelta
from pathlib import Path

import fixtures
from support import Device, Env

PS2_STATE = "SLUS-20152 (12345678).01.p2s"


class Base(Env):
    SLUG, NAME, FILE, DATA = "ps2", "PlayStation 2", "Test Game (USA).iso", None

    def setUp(self):
        super().setUp()
        self.pid = self.mock.add_platform(self.SLUG, self.NAME)
        self.rom = self.mock.add_rom(self.pid, "Test Game (USA)", [(self.FILE, self.rom_bytes())], title_id=self.title_id())
        self.a, self.b = Device(self, "handheld-a"), Device(self, "handheld-b")
        for d in (self.a, self.b):
            with d.active():
                d.install(self.rom)
                d.engine()
                self.prepare(d)

    def title_id(self):
        return "SLUS-20152"

    def rom_bytes(self):
        return fixtures.make_ps2_iso("SLUS-20152")

    def prepare(self, dev):
        pass

    def folder(self, dev) -> Path:
        return dev.home / ".config" / "ARMSX2" / "sstates"

    def put(self, dev, name, data, age=0.0):
        d = self.folder(dev)
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_bytes(data)
        t = time.time() - age
        os.utime(d / name, (t, t))

    def sync(self, dev, **kw):
        with dev.active():
            return dev.sync.sync([self.rom], **kw)[0]

    def server_states(self):
        return self.mock.states

    def posts(self):
        return [r for r in self.mock.requests if r == ("POST", "/api/states")]


class Ps2StateTests(Base):
    def test_a_state_is_uploaded_once_under_its_own_name_and_emulator(self):
        self.put(self.a, PS2_STATE, os.urandom(5000))
        self.put(self.a, PS2_STATE + ".backup", b"the emulator's own copy")
        self.put(self.a, "SLES-99999 (AAAAAAAA).01.p2s", b"another game's state")
        r = self.sync(self.a)
        self.assertIn("1 save state uploaded", r["message"], r)
        (s,) = self.server_states()
        self.assertEqual((s["file_name"], s["emulator"]), (PS2_STATE, "armsx2"))
        self.assertEqual(s["data"], (self.folder(self.a) / PS2_STATE).read_bytes())
        self.sync(self.a)
        self.assertEqual(len(self.posts()), 1, "unchanged: not sent again")
        time.sleep(0.02)
        self.put(self.a, PS2_STATE, os.urandom(5000))
        self.sync(self.a)
        self.assertEqual(len(self.posts()), 2)
        self.assertEqual(len(self.server_states()), 1, "the same name replaces the state")

    def test_another_handheld_gets_it_and_a_newer_one_replaces_the_old_after_a_backup(self):
        self.put(self.a, PS2_STATE, b"first" * 1000)
        self.sync(self.a)
        r = self.sync(self.b)
        self.assertIn("1 save state downloaded", r["message"], r)
        self.assertEqual((self.folder(self.b) / PS2_STATE).read_bytes(), b"first" * 1000)
        self.sync(self.b)
        self.assertEqual(len(self.mock.requests and [x for x in self.mock.requests if x[1].endswith("/content") and "states" in x[1]]), 1,
                         "taken once")
        time.sleep(0.05)
        self.put(self.a, PS2_STATE, b"second" * 1000)
        self.sync(self.a)
        self.sync(self.b)
        self.assertEqual((self.folder(self.b) / PS2_STATE).read_bytes(), b"second" * 1000)
        with self.b.active():
            from rommarmada import paths
            self.assertEqual((paths.backups_dir() / f"game-{self.rom}" / "states" / PS2_STATE).read_bytes(), b"first" * 1000)

    def test_a_state_deleted_here_does_not_come_back_but_a_new_device_still_gets_it(self):
        self.put(self.a, PS2_STATE, b"x" * 2000)
        self.sync(self.a)
        (self.folder(self.a) / PS2_STATE).unlink()
        self.sync(self.a)
        self.assertFalse((self.folder(self.a) / PS2_STATE).exists())
        self.assertEqual(len(self.server_states()), 1, "nothing was deleted on RomM either")
        self.sync(self.b)
        self.assertTrue((self.folder(self.b) / PS2_STATE).exists())

    def test_changed_on_both_sides_is_left_alone(self):
        self.put(self.a, PS2_STATE, b"v1" * 1000)
        self.sync(self.a)
        self.sync(self.b)
        time.sleep(0.05)
        self.put(self.a, PS2_STATE, b"a's v2" * 1000)
        self.sync(self.a)
        self.put(self.b, PS2_STATE, b"b's v2" * 1000)
        r = self.sync(self.b)
        self.assertIn("left alone", r["message"], r)
        self.assertEqual((self.folder(self.b) / PS2_STATE).read_bytes(), b"b's v2" * 1000)
        self.assertEqual(self.server_states()[0]["data"], b"a's v2" * 1000)

    def test_first_contact_the_newer_one_wins(self):
        self.mock.states.append({"id": 900, "rom_id": self.rom, "file_name": PS2_STATE, "emulator": "armsx2", "data": b"old on romm" * 100,
                                 "updated_at": self.mock.now() - timedelta(days=2), "file_size_bytes": 1100})
        self.put(self.a, PS2_STATE, b"newer here" * 100)
        self.sync(self.a)
        self.assertEqual(self.server_states()[0]["data"], b"newer here" * 100)
        self.mock.states[0].update(data=b"newest on romm" * 100, updated_at=self.mock.now() + timedelta(seconds=30))
        self.put(self.b, PS2_STATE, b"older here" * 100, age=3600)
        self.sync(self.b)
        self.assertEqual((self.folder(self.b) / PS2_STATE).read_bytes(), b"newest on romm" * 100)

    def test_other_emulators_states_and_other_games_are_ignored_and_it_can_be_turned_off(self):
        self.mock.states.append({"id": 901, "rom_id": self.rom, "file_name": PS2_STATE, "emulator": "ejs-core", "data": b"web player",
                                 "updated_at": self.mock.now(), "file_size_bytes": 10})
        self.sync(self.b)
        self.assertFalse(self.folder(self.b).exists() and any(self.folder(self.b).iterdir()))
        self.put(self.a, PS2_STATE, b"s" * 100)
        self.a.settings.update({"states": {"enabled": False}})
        self.sync(self.a)
        self.assertEqual(self.posts(), [])


class DolphinStateTests(Base):
    SLUG, NAME, FILE = "gc", "GameCube", "Test Game (USA).iso"

    def title_id(self):
        return ""

    def rom_bytes(self):
        return fixtures.make_gc_iso("GZLE")

    def folder(self, dev):
        return dev.home / ".var" / "app" / "org.DolphinEmu.dolphin-emu" / "data" / "dolphin-emu" / "StateSaves"

    def test_dolphin_states_go_up_and_come_down(self):
        self.put(self.a, "GZLE01.s01", os.urandom(3000))
        self.put(self.a, "GZLE01.s01.png", b"not a state")
        self.put(self.a, "GALE01.s01", b"another game")
        self.sync(self.a)
        (s,) = self.server_states()
        self.assertEqual((s["file_name"], s["emulator"]), ("GZLE01.s01", "dolphin"))
        r = self.sync(self.b)
        self.assertIn("downloaded", r["message"], r)
        self.assertEqual((self.folder(self.b) / "GZLE01.s01").read_bytes(), s["data"])


class RetroArchStateTests(Base):
    SLUG, NAME, FILE = "gba", "Game Boy Advance", "Test Game (USA).gba"

    def title_id(self):
        return ""

    def rom_bytes(self):
        return b"GBA" * 1000

    def prepare(self, dev):
        d = dev.home / ".config" / "retroarch"
        d.mkdir(parents=True, exist_ok=True)
        (d / "retroarch.cfg").write_text(f'savestate_directory = "{d / "states"}"\n')

    def folder(self, dev):
        return dev.home / ".config" / "retroarch" / "states"

    def test_retroarch_states_follow_the_game_name_and_the_configured_folder(self):
        self.put(self.a, "Test Game (USA).state", b"s0" * 500)
        self.put(self.a, "Test Game (USA).state3", b"s3" * 500)
        self.put(self.a, "Test Game (USA).state.auto", b"auto" * 500)
        self.put(self.a, "Test Game (USA).state.png", b"thumbnail")
        self.put(self.a, "Other Game.state", b"other")
        self.sync(self.a)
        self.assertEqual(sorted(s["file_name"] for s in self.server_states()),
                         ["Test Game (USA).state", "Test Game (USA).state.auto", "Test Game (USA).state3"])
        self.assertTrue(all(s["emulator"] == "retroarch" for s in self.server_states()))
        r = self.sync(self.b)
        self.assertIn("3 save states downloaded", r["message"], r)
        self.assertEqual((self.folder(self.b) / "Test Game (USA).state3").read_bytes(), b"s3" * 500)

    def test_a_folder_per_core_with_no_state_yet_says_so_instead_of_guessing(self):
        d = self.b.home / ".config" / "retroarch"
        (d / "retroarch.cfg").write_text(f'savestate_directory = "{d / "states"}"\nsort_savestates_enable = "true"\n')
        self.mock.states.append({"id": 950, "rom_id": self.rom, "file_name": "Test Game (USA).state", "emulator": "retroarch",
                                 "data": b"x" * 100, "updated_at": self.mock.now(), "file_size_bytes": 100})
        self.sync(self.b)
        self.assertFalse((d / "states").exists() and any((d / "states").rglob("*.state")))
