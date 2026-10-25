# SPDX-License-Identifier: MIT
"""Save sync end to end: the plugin's SaveSync, the real savebridge helper, and the mock RomM."""
import hashlib
import os
import time
from datetime import timedelta
from pathlib import Path

import fixtures
from support import BIN, Device, Env


class SyncBase(Env):
    def setUp(self):
        super().setUp()
        self.pid = self.mock.add_platform("ps2", "PlayStation 2")
        self.rom = self.mock.add_rom(self.pid, "Test Game (USA)", [("Test Game (USA).iso", fixtures.make_ps2_iso("SLUS-20152"))],
                                     title_id="SLUS-20152")
        self.a = Device(self, "handheld-a")
        self.b = Device(self, "handheld-b")
        for d in (self.a, self.b):
            with d.active():
                d.install(self.rom)
                d.engine()

    def sync(self, dev, **kw):
        with dev.active():
            out = dev.sync.sync([self.rom], **kw)
        self.assertEqual(len(out), 1)
        return out[0]

    def server_saves(self, slot="autosave"):
        return [s for s in self.mock.saves_of(self.rom, slot)]


class FirstUploadTests(SyncBase):
    def test_upload_then_nothing_to_do_then_upload_again(self):
        self.a.play(b"chapter 1" * 100)
        r = self.sync(self.a)
        self.assertEqual(r["status"], "uploaded", r)
        saves = self.server_saves()
        self.assertEqual(len(saves), 1)
        self.assertEqual(saves[0]["emulator"], "armsx2_refresh")          # the label Argosy gives ARMSX2 on Android
        self.assertIn("[", saves[0]["file_name"])                         # RomM tags slotted versions
        self.assertEqual(len(saves[0]["data"]), 8650752)                  # sigil's unit: a per-game memory card
        self.assertEqual(hashlib.md5(saves[0]["data"]).hexdigest(), saves[0]["content_hash"])
        # the server now counts this device as in sync with that save
        sy = self.mock.syncs[(self.a.device_id, saves[0]["id"])]
        self.assertEqual(sy["hash"], saves[0]["content_hash"])

        again = self.sync(self.a)
        self.assertEqual(again["status"], "up_to_date", again)
        self.assertEqual(len(self.server_saves()), 1)

        time.sleep(0.02)
        self.a.play(b"chapter 2" * 100)
        r = self.sync(self.a)
        self.assertEqual(r["status"], "uploaded", r)
        self.assertEqual(len(self.server_saves()), 2)

    def test_only_this_games_saves_travel(self):
        self.a.play(b"mine" * 50)
        self.a.play(b"someone else's", serial_folder="BESLES-99999")
        self.sync(self.a)
        # restore the unit alone on a clean card: the other game's folder is not in it
        self.b.card().parent.mkdir(parents=True, exist_ok=True)
        r = self.sync(self.b)
        self.assertEqual(r["status"], "downloaded", r)
        self.assertTrue((self.b.card() / "BASLUS-20152" / "save.dat").is_file())
        self.assertFalse((self.b.card() / "BESLES-99999").exists())

    def test_no_saves_anywhere(self):
        r = self.sync(self.a)
        self.assertEqual(r["status"], "no_saves", r)

    def test_unchanged_saves_skip_the_network_when_asked(self):
        self.a.play(b"x" * 100)
        self.sync(self.a)
        before = len(self.mock.requests)
        with self.a.active():
            r = self.a.sync.sync([self.rom], only_if_changed=True)[0]
        self.assertEqual(r["status"], "up_to_date")
        self.assertEqual(len(self.mock.requests), before)


class PullTests(SyncBase):
    def test_second_device_gets_the_save_and_is_then_in_sync(self):
        self.a.play(b"from handheld a" * 40)
        self.sync(self.a)
        r = self.sync(self.b)
        self.assertEqual(r["status"], "downloaded", r)
        self.assertEqual(self.b.save_bytes(), self.a.save_bytes())
        self.assertTrue(self.b.card().is_dir(), "the card must stay a folder card")
        # the baseline was confirmed to the server, so b is not told it has something to upload
        save = self.server_saves()[0]
        self.assertEqual(self.mock.syncs[(self.b.device_id, save["id"])]["hash"], save["content_hash"])
        again = self.sync(self.b)
        self.assertEqual(again["status"], "up_to_date", again)
        self.assertEqual(len(self.server_saves()), 1)

    def test_a_backup_of_the_card_is_made_before_it_is_written(self):
        self.a.play(b"v1" * 100)
        self.sync(self.a)
        self.b.play(b"b was here first" * 10, serial_folder="BESLES-99999")
        self.sync(self.b)
        with self.b.active():
            from rommarmada import saves
            backups = saves.list_backups(self.rom)
        self.assertEqual(len(backups), 1)
        self.assertTrue((Path(backups[0]["path"]) / "Mcd001.ps2" / "BESLES-99999" / "save.dat").is_file())

    def test_newer_server_save_replaces_an_unchanged_local_one(self):
        self.a.play(b"v1" * 100)
        self.sync(self.a)
        self.sync(self.b)
        time.sleep(0.02)
        self.a.play(b"v2" * 100)
        self.sync(self.a)
        r = self.sync(self.b)
        self.assertEqual(r["status"], "downloaded", r)
        self.assertEqual(self.b.save_bytes(), b"v2" * 100)


class ConflictTests(SyncBase):
    def diverge(self):
        self.a.play(b"base" * 100)
        self.sync(self.a)
        self.sync(self.b)
        time.sleep(0.02)
        self.a.play(b"a played" * 100)
        self.b.play(b"b played" * 100)
        self.sync(self.a)                       # a gets there first

    def test_both_changed_asks_and_changes_nothing(self):
        self.diverge()
        r = self.sync(self.b)
        self.assertEqual(r["status"], "conflict", r)
        self.assertEqual(r["conflict"]["reason"], "both_changed")
        self.assertEqual(self.b.save_bytes(), b"b played" * 100)
        self.assertEqual(len(self.server_saves()), 2)                    # nothing was overwritten or added

    def test_choose_the_server_save(self):
        self.diverge()
        self.sync(self.b)
        r = self.sync(self.b, resolutions={self.rom: "server"})
        self.assertEqual(r["status"], "downloaded", r)
        self.assertEqual(self.b.save_bytes(), b"a played" * 100)
        self.assertEqual(self.sync(self.b)["status"], "up_to_date")

    def test_choose_this_device(self):
        self.diverge()
        self.sync(self.b)
        r = self.sync(self.b, resolutions={self.rom: "device"})
        self.assertEqual(r["status"], "uploaded", r)
        self.assertEqual(self.b.save_bytes(), b"b played" * 100)
        newest = max(self.server_saves(), key=lambda s: s["updated_at"])
        self.assertEqual(newest["origin_device_id"], self.b.device_id)
        self.assertEqual(self.sync(self.a)["status"], "downloaded")      # and a is then offered b's

    def test_keep_both_saves_this_devices_copy_in_its_own_slot(self):
        self.diverge()
        self.sync(self.b)
        r = self.sync(self.b, resolutions={self.rom: "keep_both"})
        self.assertEqual(r["status"], "downloaded", r)
        self.assertEqual(self.b.save_bytes(), b"a played" * 100)
        other = [s for s in self.mock.saves if s["rom_id"] == self.rom and s["slot"].startswith("device-")]
        self.assertEqual(len(other), 1)
        self.assertEqual(len(other[0]["data"]), 8650752)

    def test_a_device_that_never_synced_does_not_overwrite_on_a_guess(self):
        # the server has a save from elsewhere; this device has its own, and no history with this game
        self.mock.seed_save(self.rom, b"X" * 8650752, age=timedelta(days=30))
        self.a.play(b"my own progress" * 40)
        r = self.sync(self.a)
        self.assertEqual(r["status"], "conflict", r)
        self.assertEqual(r["conflict"]["reason"], "first_contact")
        self.assertEqual(self.a.save_bytes(), b"my own progress" * 40)
        self.assertEqual(len(self.server_saves()), 1)

    def test_conflict_policy_can_decide_for_you(self):
        self.mock.seed_save(self.rom, b"X" * 8650752, age=timedelta(days=30))
        self.a.play(b"my own progress" * 40)
        self.a.settings.update({"saves": {"conflict": "device"}})
        self.assertEqual(self.sync(self.a)["status"], "uploaded")
        self.assertEqual(len(self.server_saves()), 2)

    def test_pending_conflict_is_remembered(self):
        self.diverge()
        self.sync(self.b)
        with self.b.active():
            st = self.b.sync.status(self.rom)
        self.assertEqual(st["conflict"]["reason"], "both_changed")


class SafetyTests(SyncBase):
    def fake_proc(self, running=True):
        proc = self.tmp / "proc"
        if running:
            d = proc / "4242"
            d.mkdir(parents=True)
            (d / "comm").write_text("armsx2-qt\n")
            (d / "cmdline").write_bytes(b"armsx2-qt\0")
            os.symlink("/usr/bin/armsx2-qt", d / "exe")
        else:
            __import__("shutil").rmtree(proc, ignore_errors=True)
            proc.mkdir(exist_ok=True)
        return str(proc)

    def test_nothing_is_touched_while_the_emulator_runs(self):
        self.a.play(b"x" * 100)
        with self.a.active():
            self.a.engine(proc_root=self.fake_proc(True))
        r = self.sync(self.a)
        self.assertEqual(r["status"], "busy", r)
        self.assertEqual(self.mock.saves, [])
        with self.a.active():
            self.a.engine(proc_root=self.fake_proc(False))
        self.assertEqual(self.sync(self.a)["status"], "uploaded")

    def test_server_down_leaves_the_card_alone(self):
        self.a.play(b"x" * 100)
        self.mock.stop()
        r = self.sync(self.a)
        self.assertEqual(r["status"], "offline", r)
        self.assertEqual(self.a.save_bytes(), b"x" * 100)

    def test_a_corrupt_download_is_not_restored(self):
        self.a.play(b"x" * 100)
        self.sync(self.a)
        s = self.server_saves()[0]
        s["data"] = s["data"][:1000]                                    # truncated on the server; hash no longer matches
        r = self.sync(self.b)
        self.assertEqual(r["status"], "error", r)
        self.assertFalse(self.b.card().exists() and (self.b.card() / "BASLUS-20152").exists())

    def test_sync_off_does_nothing(self):
        self.a.settings.update({"saves": {"enabled": False}})
        self.a.play(b"x" * 100)
        self.assertEqual(self.sync(self.a)["status"], "disabled")

    def test_signed_out(self):
        self.a.settings.clear_auth()
        self.assertEqual(self.sync(self.a)["status"], "auth")

    def test_device_unknown_to_the_server_is_registered_again(self):
        calls = []
        self.a.play(b"x" * 100)
        del self.mock.devices[self.a.device_id]
        with self.a.active():
            new = "dev-new"
            self.mock.devices[new] = {"id": new, "name": "re", "hostname": "re", "platform": "linux", "sync_enabled": True,
                                      "created_at": "2026-01-01T00:00:00Z"}

            def reregister():
                calls.append(1)
                self.a.settings.update({"auth": {"device_id": new}})
                return new
            self.a.sync.reregister = reregister
            r = self.a.sync.sync([self.rom])[0]
        self.assertEqual(r["status"], "uploaded", r)
        self.assertEqual(calls, [1])

    def test_unsupported_system(self):
        pid = self.mock.add_platform("snes", "Super Nintendo")
        rid = self.mock.add_rom(pid, "Some SNES Game", [("Some SNES Game.sfc", b"rom")])
        with self.a.active():
            self.a.install(rid)
            r = self.a.sync.sync([rid])[0]
        self.assertEqual(r["status"], "unsupported")


class FolderCardPolicyTests(SyncBase):
    def run_sync(self, dev=None):
        return self.sync(dev or self.a)

    def test_a_missing_card_becomes_a_folder_card(self):
        self.mock.seed_save(self.rom, b"?" * 10)                          # irrelevant bytes; just so there is a download
        self.a.play(b"x" * 100)
        shutil_rm = __import__("shutil").rmtree
        shutil_rm(self.a.card())                                          # the emulator has made no card yet
        self.sync(self.a)
        self.assertTrue(self.a.card().is_dir())
        self.assertTrue((self.a.card() / "_pcsx2_superblock").is_file())

    def test_a_download_onto_a_fresh_device_makes_a_folder_card_not_a_file(self):
        self.a.play(b"hello" * 100)
        self.sync(self.a)
        self.assertEqual(self.sync(self.b)["status"], "downloaded")
        self.assertTrue(self.b.card().is_dir())
        self.assertTrue((self.b.card() / "_pcsx2_superblock").is_file())
        self.assertTrue((self.b.card() / "BASLUS-20152" / "save.dat").is_file())

    def test_an_empty_file_card_is_replaced_by_a_folder_card(self):
        self.a.play(b"hello" * 100)
        self.sync(self.a)
        card = self.b.card()
        card.parent.mkdir(parents=True)
        card.write_bytes(b"\xff" * 8650752)                               # what the emulator makes for an unused slot
        self.assertEqual(self.sync(self.b)["status"], "downloaded")
        self.assertTrue(card.is_dir())
        self.assertTrue(any(p.name.startswith("Mcd001.ps2.empty-") for p in card.parent.iterdir()))   # kept, not deleted

    def test_a_file_card_with_saves_is_never_touched_under_the_folder_policy(self):
        self.a.play(b"hello" * 100)
        self.sync(self.a)
        # b has a real file card with a save on it (built by sigil from a zip)
        with self.b.active():
            sb = self.savebridge()
            rom = self.b.manifest.get(self.rom)["main_file"]
            unit = fixtures.write(str(self.tmp / "u.zip"), fixtures.make_save_zip("BASLUS-20152"))
            root = str(self.b.home / ".config" / "ARMSX2")
            os.makedirs(root, exist_ok=True)
            sb.restore(layout="pcsx2_standalone", root=root, unit=Path(unit), rom=rom, platform_slug="ps2")
        card = self.b.card()
        self.assertTrue(card.is_file())
        before = card.read_bytes()
        r = self.sync(self.b)
        self.assertEqual(r["status"], "blocked", r)
        self.assertEqual(r["reason"], "file_card")
        self.assertIn("Convert", r["message"])
        self.assertEqual(card.read_bytes(), before)

    def test_auto_policy_works_with_whatever_the_emulator_has(self):
        self.a.play(b"hello" * 100)
        self.sync(self.a)
        with self.b.active():
            sb = self.savebridge()
            rom = self.b.manifest.get(self.rom)["main_file"]
            unit = fixtures.write(str(self.tmp / "u.zip"), fixtures.make_save_zip("BESLES-77777"))
            root = str(self.b.home / ".config" / "ARMSX2")
            os.makedirs(root, exist_ok=True)
            sb.restore(layout="pcsx2_standalone", root=root, unit=Path(unit),
                       rom=None, platform_slug="ps2", content="/x/Other Game.iso",
                       identity={"platform": "ps2", "title_id": "SLES-77777", "save_id": "BESLES-77777"})
        self.b.settings.update({"saves": {"card_type": "auto"}})
        r = self.sync(self.b)
        self.assertEqual(r["status"], "downloaded", r)
        self.assertTrue(self.b.card().is_file())                            # still the file card the user chose

    def test_a_card_folder_moved_elsewhere_is_explained(self):
        ini = self.a.home / ".config" / "ARMSX2" / "inis" / "PCSX2.ini"
        ini.parent.mkdir(parents=True)
        ini.write_text(f"[Folders]\nMemoryCards = {self.tmp}/moved\n")
        r = self.sync(self.a)
        self.assertEqual((r["status"], r["reason"]), ("blocked", "custom_dir"))


class DamagedCardTests(SyncBase):
    def test_a_folder_card_with_saves_but_no_superblock_is_not_touched(self):
        folder = self.a.card() / "BASLUS-20152"
        folder.mkdir(parents=True)
        (folder / "save.dat").write_bytes(b"precious")
        r = self.sync(self.a)
        self.assertEqual((r["status"], r["reason"]), ("blocked", "damaged_folder_card"))
        self.assertEqual((folder / "save.dat").read_bytes(), b"precious")
        self.assertEqual(self.mock.saves, [])
