# SPDX-License-Identifier: MIT
"""BIOS files from RomM's firmware list into the emulator's BIOS folder."""
import hashlib
import os
from pathlib import Path

from support import Env
from test_service import SvcBase

MIB = 1024 * 1024


def bios_bytes(seed: str, size: int = 4 * MIB) -> bytes:
    """A deterministic stand-in for a BIOS image (the plugin only cares about size and hashes)."""
    block = hashlib.sha256(seed.encode()).digest()
    return (block * (size // len(block) + 1))[:size]


class BiosBase(SvcBase):
    def setUp(self):
        super().setUp()
        self.a = bios_bytes("scph-70012")
        self.b = bios_bytes("scph-39001")
        self.fa = self.mock.add_firmware(self.pid, "scph70012.bin", self.a, verified=True)
        self.fb = self.mock.add_firmware(self.pid, "scph39001.bin", self.b, verified=False)
        self.folder = self.home / ".config" / "ARMSX2" / "bios"

    def names(self):
        return sorted(p.name for p in self.folder.iterdir()) if self.folder.is_dir() else []

    def downloads_of_firmware(self):
        return [p for m, p in self.mock.requests if p.startswith("/api/firmware/") and "/content/" in p]


class StatusTests(BiosBase):
    def test_status_lists_what_romm_has_verified_first(self):
        st = self.svc.bios_status()
        self.assertEqual(st["emulator"]["name"], "ARMSX2")
        self.assertEqual(st["dir"], str(self.folder))
        self.assertFalse(st["has_bios"])
        self.assertTrue(st["remote"]["ok"])
        self.assertEqual(st["remote"]["platform"]["name"], "PlayStation 2")
        self.assertEqual([i["name"] for i in st["remote"]["items"]], ["scph70012.bin", "scph39001.bin"])
        self.assertEqual({i["state"] for i in st["remote"]["items"]}, {"missing"})
        self.assertEqual(st["remote"]["items"][0]["size"], 4 * MIB)

    def test_status_without_asking_the_server(self):
        before = len(self.mock.requests)
        st = self.svc.bios_status(remote=False)
        self.assertIsNone(st["remote"])
        self.assertEqual(len(self.mock.requests), before)

    def test_overview_says_whether_a_bios_is_there(self):
        self.assertEqual(self.svc.overview()["emulator"]["bios"], {"dir": str(self.folder), "has_bios": False, "count": 0})
        self.folder.mkdir(parents=True)
        (self.folder / "readme.txt").write_text("not a bios")
        (self.folder / "scph70012.bin.rom1").write_bytes(b"x" * 1000)
        (self.folder / "tiny.bin").write_bytes(b"x" * (MIB))
        self.assertFalse(self.svc.overview()["emulator"]["bios"]["has_bios"])      # none of these is a BIOS image
        (self.folder / "scph70012.bin").write_bytes(self.a)
        o = self.svc.overview()["emulator"]["bios"]
        self.assertEqual((o["has_bios"], o["count"]), (True, 1))

    def test_the_emulators_own_bios_folder_setting_is_followed(self):
        data = self.home / ".config" / "ARMSX2"
        (data / "inis").mkdir(parents=True)
        (data / "inis" / "PCSX2.ini").write_text(f"[Folders]\nBios = {self.home / 'my-bios'}\n")
        self.assertEqual(self.svc.bios_status(False)["dir"], str(self.home / "my-bios"))
        done = self.svc.bios_install()
        self.assertTrue(done["ok"], done)
        self.assertTrue((self.home / "my-bios" / "scph70012.bin").is_file())
        # a relative setting is relative to the data folder
        (data / "inis" / "PCSX2.ini").write_text("[Folders]\nBios = firmware\n")
        self.assertEqual(self.svc.bios_status(False)["dir"], str(data / "firmware"))

    def test_firmware_romm_lost_from_its_disk_is_not_offered(self):
        self.mock.add_firmware(self.pid, "gone.bin", bios_bytes("gone"), missing=True)
        names = [i["name"] for i in self.svc.bios_status()["remote"]["items"]]
        self.assertNotIn("gone.bin", names)

    def test_no_playstation_2_library_in_romm(self):
        self.mock.platforms.clear()
        self.mock.firmware.clear()
        self.mock.add_platform("snes", "Super Nintendo")
        st = self.svc.bios_status()
        self.assertTrue(st["remote"]["ok"])
        self.assertEqual(st["remote"]["reason"], "no_platform")
        done = self.svc.bios_install()
        self.assertFalse(done["ok"])
        self.assertIn("no PlayStation 2", done["message"])

    def test_romm_with_no_firmware_says_where_to_add_it(self):
        self.mock.firmware.clear()
        st = self.svc.bios_status()
        self.assertEqual(st["remote"]["reason"], "empty")
        self.assertIn("Firmware", st["remote"]["message"])
        self.assertFalse(self.svc.bios_install()["ok"])

    def test_firmware_for_another_platform_is_not_offered(self):
        other = self.mock.add_platform("psx", "PlayStation")
        self.mock.add_firmware(other, "scph1001.bin", bios_bytes("psx", 512 * 1024))
        self.assertEqual(len(self.svc.bios_status()["remote"]["items"]), 2)

    def test_a_token_without_firmware_access_is_explained(self):
        self.sign_in(scopes=[s for s in __import__("mock_romm").ALL_SCOPES if s != "firmware.read"])
        st = self.svc.bios_status()
        self.assertFalse(st["remote"]["ok"])
        self.assertEqual(st["remote"]["reason"], "forbidden")
        self.assertIn("pair again", st["remote"]["message"])
        done = self.svc.bios_install()
        self.assertFalse(done["ok"])
        self.assertEqual(self.names(), [])
        self.assertTrue(st["local"] == [] and not st["has_bios"])      # the local half still works without it

    def test_the_server_being_away_leaves_the_local_half_working(self):
        self.mock.stop()
        st = self.svc.bios_status()
        self.assertFalse(st["remote"]["ok"])
        self.assertEqual(st["remote"]["reason"], "network")
        self.assertEqual(st["dir"], str(self.folder))


class InstallTests(BiosBase):
    def test_installs_everything_and_checks_it(self):
        self.assertFalse(self.folder.exists())          # the emulator was never started: the folder is made
        done = self.svc.bios_install()
        self.assertTrue(done["ok"], done)
        self.assertEqual(sorted(done["report"]["installed"]), ["scph39001.bin", "scph70012.bin"])
        self.assertEqual((self.folder / "scph70012.bin").read_bytes(), self.a)
        self.assertEqual((self.folder / "scph39001.bin").read_bytes(), self.b)
        self.assertEqual(self.names(), ["scph39001.bin", "scph70012.bin"])         # no .part files left
        self.assertIn("Installed 2 files", done["message"])
        self.assertTrue(done["status"]["has_bios"])
        self.assertEqual({i["state"] for i in done["status"]["remote"]["items"]}, {"installed"})

    def test_one_file_by_id(self):
        done = self.svc.bios_install(ids=[self.fb])
        self.assertTrue(done["ok"], done)
        self.assertEqual(self.names(), ["scph39001.bin"])
        self.assertEqual(len(self.downloads_of_firmware()), 1)

    def test_what_is_already_there_is_not_downloaded_again(self):
        self.folder.mkdir(parents=True)
        (self.folder / "scph70012.bin").write_bytes(self.a)
        done = self.svc.bios_install()
        self.assertEqual(done["report"]["installed"], ["scph39001.bin"])
        self.assertEqual(done["report"]["skipped"], ["scph70012.bin"])
        self.assertEqual(len(self.downloads_of_firmware()), 1)
        again = self.svc.bios_install()
        self.assertTrue(again["ok"])
        self.assertEqual(again["report"]["installed"], [])
        self.assertIn("already there", again["message"])
        self.assertEqual(len(self.downloads_of_firmware()), 1)

    def test_a_copy_you_renamed_counts_as_there(self):
        """The player downloaded the BIOS by hand and gave it another name: no second copy next to it."""
        self.folder.mkdir(parents=True)
        (self.folder / "my ps2 bios.bin").write_bytes(self.a)
        st = self.svc.bios_status()
        item = next(i for i in st["remote"]["items"] if i["name"] == "scph70012.bin")
        self.assertEqual((item["state"], item["held_as"]), ("present", "my ps2 bios.bin"))
        done = self.svc.bios_install()
        self.assertEqual(done["report"]["installed"], ["scph39001.bin"])
        self.assertEqual(self.names(), ["my ps2 bios.bin", "scph39001.bin"])

    def test_a_half_finished_download_left_by_a_crash_is_not_taken_for_a_bios(self):
        self.folder.mkdir(parents=True)
        (self.folder / ".scph70012.bin.part").write_bytes(self.a)          # complete, but never renamed into place
        st = self.svc.bios_status()
        item = next(i for i in st["remote"]["items"] if i["name"] == "scph70012.bin")
        self.assertEqual(item["state"], "missing")
        self.assertFalse(st["has_bios"], "the emulator would not use a hidden .part file either")
        done = self.svc.bios_install(ids=[self.fa])
        self.assertTrue(done["ok"], done)
        self.assertEqual((self.folder / "scph70012.bin").read_bytes(), self.a)

    def test_a_different_file_with_the_same_name_is_kept(self):
        self.folder.mkdir(parents=True)
        mine = bios_bytes("my own patched bios")
        (self.folder / "scph70012.bin").write_bytes(mine)
        done = self.svc.bios_install()
        self.assertEqual(done["report"]["conflicts"], ["scph70012.bin"])
        self.assertEqual((self.folder / "scph70012.bin").read_bytes(), mine)
        self.assertEqual(done["report"]["installed"], ["scph39001.bin"])
        self.assertIn("kept", done["message"])
        item = next(i for i in done["status"]["remote"]["items"] if i["name"] == "scph70012.bin")
        self.assertEqual(item["state"], "different")

    def test_a_different_file_is_replaced_only_when_asked_for_by_name_and_is_backed_up(self):
        self.folder.mkdir(parents=True)
        mine = bios_bytes("my own patched bios")
        (self.folder / "scph70012.bin").write_bytes(mine)
        # "install everything" never replaces, even with overwrite set
        self.svc.bios_install(ids=None, overwrite=True)
        self.assertEqual((self.folder / "scph70012.bin").read_bytes(), mine)
        done = self.svc.bios_install(ids=[self.fa], overwrite=True)
        self.assertTrue(done["ok"], done)
        self.assertEqual(done["report"]["installed"], ["scph70012.bin"])
        self.assertEqual((self.folder / "scph70012.bin").read_bytes(), self.a)
        from rommarmada import paths
        saved = list((paths.backups_dir() / "bios").iterdir())
        self.assertEqual(len(saved), 1)
        self.assertTrue(saved[0].name.endswith("-scph70012.bin"))
        self.assertEqual(saved[0].read_bytes(), mine)

    def test_replacing_waits_for_the_emulator_to_close(self):
        self.folder.mkdir(parents=True)
        mine = bios_bytes("my own patched bios")
        (self.folder / "scph70012.bin").write_bytes(mine)
        self.start_emulator()
        done = self.svc.bios_install(ids=[self.fa], overwrite=True)
        self.assertFalse(done["ok"])
        self.assertEqual(done["report"]["busy"], ["scph70012.bin"])
        self.assertIn("Close the emulator", done["message"])
        self.assertEqual((self.folder / "scph70012.bin").read_bytes(), mine)
        # adding a file that is not there yet is fine while it runs
        ok = self.svc.bios_install(ids=[self.fb])
        self.assertTrue(ok["ok"], ok)
        self.stop_emulator()
        self.assertTrue(self.svc.bios_install(ids=[self.fa], overwrite=True)["ok"])

    def test_a_broken_transfer_leaves_nothing_behind_and_can_be_repeated(self):
        self.mock.drop_after = 100_000
        done = self.svc.bios_install(ids=[self.fa])
        self.assertFalse(done["ok"])
        self.assertEqual(done["report"]["failed"][0]["name"], "scph70012.bin")
        self.assertEqual(done["report"]["failed"][0]["kind"], "network")
        self.assertEqual(self.names(), [])
        again = self.svc.bios_install(ids=[self.fa])
        self.assertTrue(again["ok"], again)
        self.assertEqual((self.folder / "scph70012.bin").read_bytes(), self.a)

    def test_a_file_that_does_not_match_romms_checksum_is_refused(self):
        self.mock.firmware[self.fa]["data"] = bios_bytes("corrupted in transit")
        done = self.svc.bios_install(ids=[self.fa])
        self.assertFalse(done["ok"])
        self.assertIn("checksum", done["report"]["failed"][0]["error"])
        self.assertEqual(done["report"]["failed"][0]["kind"], "bad_file")
        self.assertEqual(self.names(), [])

    def test_a_refused_replacement_keeps_the_old_file(self):
        self.folder.mkdir(parents=True)
        mine = bios_bytes("my own patched bios")
        (self.folder / "scph70012.bin").write_bytes(mine)
        self.mock.firmware[self.fa]["data"] = bios_bytes("corrupted in transit")
        done = self.svc.bios_install(ids=[self.fa], overwrite=True)
        self.assertFalse(done["ok"])
        self.assertEqual((self.folder / "scph70012.bin").read_bytes(), mine)
        self.assertEqual(self.names(), ["scph70012.bin"])

    def test_a_file_that_is_longer_than_romm_says_is_refused(self):
        self.mock.firmware[self.fa]["data"] = self.a + b"extra"
        done = self.svc.bios_install(ids=[self.fa])
        self.assertFalse(done["ok"])
        self.assertEqual(self.names(), [])

    def test_a_file_romm_no_longer_has(self):
        del self.mock.firmware[self.fa]
        done = self.svc.bios_install(ids=[self.fa])
        self.assertFalse(done["ok"])
        self.assertEqual(done["error"], "gone")

    def test_a_file_removed_between_listing_and_download(self):
        orig = self.mock.send_blob

        def vanish(h, data):
            self.mock.firmware.pop(self.fa, None)
            from mock_romm import Fail
            raise Fail(404, "File not found")
        self.mock.send_blob = vanish
        try:
            done = self.svc.bios_install(ids=[self.fa])
        finally:
            self.mock.send_blob = orig
        self.assertFalse(done["ok"])
        self.assertIn("does not have this file", done["report"]["failed"][0]["error"])

    def test_a_server_that_wants_a_new_sign_in_stops_the_run(self):
        self.mock.tokens.clear()
        done = self.svc.bios_install()
        self.assertFalse(done["ok"])
        self.assertTrue(any(e == "auth_required" for e, _ in self.events))
        self.assertEqual(self.names(), [])

    def test_file_names_cannot_leave_the_folder(self):
        evil = self.mock.add_firmware(self.pid, "../../evil.bin", bios_bytes("evil"))
        done = self.svc.bios_install(ids=[evil])
        self.assertTrue(done["ok"], done)
        self.assertFalse((self.home / ".config" / "evil.bin").exists())
        self.assertFalse((self.home / "evil.bin").exists())
        self.assertEqual(len(self.names()), 1)

    def test_not_enough_room_changes_nothing(self):
        import rommarmada.bios as biosmod
        real = biosmod.free_bytes
        biosmod.free_bytes = lambda p: 1 * MIB
        try:
            done = self.svc.bios_install()
        finally:
            biosmod.free_bytes = real
        self.assertFalse(done["ok"])
        self.assertEqual(done["error"], "space")
        self.assertEqual(self.names(), [])

    def test_a_folder_that_cannot_be_written_is_explained(self):
        self.folder.mkdir(parents=True)
        self.folder.chmod(0o500)
        try:
            if os.access(self.folder, os.W_OK):
                self.skipTest("running as a user that ignores permissions")
            done = self.svc.bios_install()
        finally:
            self.folder.chmod(0o700)
        self.assertFalse(done["ok"])
        self.assertTrue(done["report"]["failed"])
        self.assertIn("scph", done["message"])

    def test_two_installs_at_once_do_not_collide(self):
        self.assertTrue(self.svc._bios_lock.acquire(blocking=False))
        try:
            done = self.svc.bios_install()
        finally:
            self.svc._bios_lock.release()
        self.assertFalse(done["ok"])
        self.assertIn("already", done["message"])
        self.assertEqual(self.names(), [])

    def test_the_summary_tells_where_the_files_went(self):
        done = self.svc.bios_install()
        self.assertIn(str(self.folder), done["message"])


class ScopeTests(Env):
    """Pairing asks for firmware access, and a token without it is flagged when signing in."""

    def test_pairing_asks_for_firmware_access_and_a_token_without_it_is_flagged(self):
        from rommarmada.connect import Connector
        from rommarmada.romm import PAIRING_SCOPES
        self.assertIn("firmware.read", PAIRING_SCOPES)
        code = self.mock.issue_pair_code(scopes=[s for s in PAIRING_SCOPES if s != "firmware.read"] + ["me.write"])
        done = Connector(self.settings).pair_with_code(self.url, code)
        self.assertEqual(done["missing_scopes"], ["firmware.read"])
        self.settings.clear_auth()
        done = Connector(self.settings).pair_with_code(self.url, self.mock.issue_pair_code())
        self.assertEqual(done["missing_scopes"], [])
