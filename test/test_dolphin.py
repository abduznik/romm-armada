# SPDX-License-Identifier: MIT
"""Dolphin: GameCube saves as .gci files in the GCI folder, synced with RomM through sigil."""
import time
from datetime import timedelta
from pathlib import Path

import fixtures
from support import Device, Env

GAME = "Test Game (USA)"


class ResolveTests(Env):
    def test_the_flatpak_user_folder_is_found_and_the_ini_next_to_it_is_read(self):
        from rommarmada import emulators
        user = self.home / ".var" / "app" / "org.DolphinEmu.dolphin-emu" / "data" / "dolphin-emu"
        cfg = self.home / ".var" / "app" / "org.DolphinEmu.dolphin-emu" / "config" / "dolphin-emu"
        user.mkdir(parents=True)
        cfg.mkdir(parents=True)
        self.assertEqual(emulators.dolphin_options(user), {})
        (cfg / "Dolphin.ini").write_text("[Core]\nSlotA = 1\nMemoryCardSize = 3\nOther = x\n")
        r = emulators.resolve_dolphin()
        self.assertEqual((r.data_dir, r.why, r.data_dir_exists), (user, "found", True))
        self.assertEqual(emulators.dolphin_options(user), {"SlotA": "1", "MemoryCardSize": "3"})
        self.assertEqual(emulators.resolve_dolphin(str(self.tmp / "mine")).why, "setting")

    def test_gamecube_slugs_belong_to_dolphin(self):
        from rommarmada import emulators
        for slug in ("gc", "ngc", "gamecube"):
            self.assertEqual(emulators.group_for_platform(slug), "dolphin")
        self.assertIn("dolphin-emu", emulators.process_names_for("dolphin"))
        self.assertNotIn("dolphin-emu", emulators.all_ps2_process_names())


class SyncBase(Env):
    def setUp(self):
        super().setUp()
        self.pid = self.mock.add_platform("gc", "GameCube")
        self.rom = self.mock.add_rom(self.pid, GAME, [(f"{GAME}.iso", fixtures.make_gc_iso("GZLE"))])
        self.a, self.b = Device(self, "handheld-a"), Device(self, "handheld-b")
        for d in (self.a, self.b):
            with d.active():
                d.install(self.rom)
                d.engine()

    def card(self, dev) -> Path:
        return dev.home / ".var" / "app" / "org.DolphinEmu.dolphin-emu" / "data" / "dolphin-emu" / "GC" / "USA" / "Card A"

    def play(self, dev, seed: int, game_id="GZLE", name="TestSave"):
        c = self.card(dev)
        c.mkdir(parents=True, exist_ok=True)
        (c / f"01-{game_id}-{name}.gci").write_bytes(fixtures.make_gci(game_id, name, seed))
        time.sleep(0.02)

    def saved(self, dev, game_id="GZLE", name="TestSave") -> bytes:
        """The save's data and name. Dolphin's own bookkeeping in the entry (the block it starts at) differs between cards."""
        raw = (self.card(dev) / f"01-{game_id}-{name}.gci").read_bytes()
        return raw[:0x36] + raw[0x38:]

    def sync(self, dev, **kw):
        with dev.active():
            out = dev.sync.sync([self.rom], **kw)
        return out[0]

    def server_saves(self, slot="armadaos"):
        return self.mock.saves_of(self.rom, slot)


class SyncTests(SyncBase):
    def test_the_game_save_goes_up_and_the_other_game_stays_home(self):
        self.play(self.a, 7)
        self.play(self.a, 9, game_id="GALE", name="Other")
        r = self.sync(self.a)
        self.assertEqual(r["status"], "uploaded", r)
        (s,) = self.server_saves()
        self.assertEqual((s["slot"], s["emulator"]), ("armadaos", "armadaos"))
        self.assertIn("GZLE", s["file_name"])
        self.assertEqual(s["data"][:4], b"GZLE", "the game's own .gci, not the other game's")
        self.assertEqual(self.sync(self.a)["status"], "up_to_date")

    def test_a_second_handheld_gets_the_save_into_its_gci_folder(self):
        self.play(self.a, 7)
        self.sync(self.a)
        r = self.sync(self.b)
        self.assertEqual(r["status"], "downloaded", r)
        self.assertEqual(self.saved(self.b), self.saved(self.a))
        self.assertEqual(self.sync(self.b)["status"], "up_to_date")
        time.sleep(0.02)
        self.play(self.a, 11)
        self.sync(self.a)
        self.assertEqual(self.sync(self.b)["status"], "downloaded")
        self.assertEqual(self.saved(self.b), self.saved(self.a))

    def test_both_changed_asks_and_a_backup_is_made_before_replacing(self):
        self.play(self.a, 7)
        self.sync(self.a)
        self.sync(self.b)
        time.sleep(0.02)
        self.play(self.a, 11)
        self.sync(self.a)
        self.play(self.b, 13)
        mine = self.saved(self.b)
        self.assertEqual(self.sync(self.b)["status"], "conflict")
        self.assertEqual(self.saved(self.b), mine)
        self.assertEqual(self.sync(self.b, resolutions={self.rom: "server"})["status"], "downloaded")
        self.assertEqual(self.saved(self.b), self.saved(self.a))
        with self.b.active():
            from rommarmada import saves
            self.assertEqual(len(saves.list_backups(self.rom)), 1)

    def test_a_save_from_another_slot_is_pulled(self):
        self.play(self.a, 7)
        self.sync(self.a)
        unit = self.server_saves()[0]["data"]
        self.mock.saves.clear()
        self.mock.seed_save(self.rom, unit, slot="argosy", emulator="dolphin", file_name="01-GZLE-TestSave.gci", age=timedelta(minutes=3))
        r = self.sync(self.b)
        self.assertEqual(r["status"], "downloaded", r)
        self.assertEqual(self.saved(self.b), self.saved(self.a))

    def test_the_game_screen_and_status(self):
        with self.a.active():
            st = self.a.sync.status(self.rom)
        self.assertEqual((st["supported"], st["emulator"]), (True, "Dolphin"))

    def test_a_ps2_emulator_running_does_not_hold_back_a_gamecube_sync(self):
        self.play(self.a, 7)
        proc = self.tmp / "proc"
        d = proc / "9"
        d.mkdir(parents=True)
        (d / "comm").write_text("armsx2-qt\n")
        (d / "cmdline").write_bytes(b"armsx2-qt\0")
        with self.a.active():
            self.a.engine(proc_root=str(proc))
        self.assertEqual(self.sync(self.a)["status"], "uploaded")
        (d / "comm").write_text("dolphin-emu\n")
        (d / "cmdline").write_bytes(b"dolphin-emu\0-b\0-e\0game.iso\0")
        self.play(self.a, 21)
        self.assertEqual(self.sync(self.a)["status"], "busy")
