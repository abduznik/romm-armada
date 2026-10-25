# SPDX-License-Identifier: MIT
import os
import time
import zipfile
import io
from pathlib import Path

import fixtures
from support import BIN, Env


class DlBase(Env):
    def setUp(self):
        super().setUp()
        self.sign_in()
        self.pid = self.ps2_platform()
        from rommarmada.downloads import DownloadManager
        self.installed = []
        self.dm = DownloadManager(self.settings, self.manifest, self.client, self.emit, self.installed.append)
        self.dm.start()

    def tearDown(self):
        self.dm.stop()
        super().tearDown()

    def rom(self, rid):
        return self.client().rom(rid)

    def wait(self, rid, states=("done", "error", "cancelled", "paused"), timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            j = self.dm.get(rid)
            if j and j["state"] in states:
                return j
            time.sleep(0.05)
        self.fail(f"download of {rid} did not finish: {self.dm.get(rid)}")

    def big(self, n=3 * 1024 * 1024):
        return os.urandom(n)


class BasicTests(DlBase):
    def test_a_game_lands_in_the_es_de_folder_and_is_registered(self):
        data = fixtures.make_ps2_iso("SLUS-20152", extra_bytes=2 * 1024 * 1024)
        rid = self.mock.add_rom(self.pid, "Test Game (USA)", [("Test Game (USA).iso", data)], title_id="SLUS-20152")
        self.dm.enqueue(self.rom(rid))
        j = self.wait(rid)
        self.assertEqual(j["state"], "done", j)
        dest = self.home / "ROMs" / "ps2" / "Test Game (USA).iso"
        self.assertEqual(dest.read_bytes(), data)
        self.assertFalse(Path(str(dest) + ".part").exists())
        entry = self.manifest.get(rid)
        self.assertEqual(entry["main_file"], str(dest))
        self.assertEqual(entry["size"], len(data))
        self.assertEqual(self.installed[0]["rom_id"], rid)
        # progress events were sent, ending in done
        states = [p["state"] for e, p in self.events if e == "download_progress" and p["rom_id"] == rid]
        self.assertEqual(states[-1], "done")

    def test_folder_style_follows_the_setting(self):
        rid = self.mock.add_rom(self.pid, "Other", [("Other.iso", b"x" * 1000)])
        self.settings.update({"library": {"folder_style": "romm", "platform_dirs": {"ps2": "PlayStation 2"}}})
        self.dm.enqueue(self.rom(rid))
        self.wait(rid)
        self.assertTrue((self.home / "ROMs" / "PlayStation 2" / "Other.iso").is_file())

    def test_multi_file_games_keep_their_folder(self):
        rid = self.mock.add_rom(self.pid, "Disc Game", [("Disc Game.m3u", b"a.chd\nb.chd\n"), ("a.chd", b"AAAA" * 100),
                                                        ("extra/b.chd", b"BBBB" * 100)])
        self.dm.enqueue(self.rom(rid))
        self.assertEqual(self.wait(rid)["state"], "done")
        base = self.home / "ROMs" / "ps2" / "Disc Game"
        self.assertEqual((base / "extra" / "b.chd").read_bytes(), b"BBBB" * 100)
        self.assertEqual(self.manifest.get(rid)["main_file"], str(base / "Disc Game.m3u"))

    def test_extras_like_manuals_are_skipped(self):
        rid = self.mock.add_rom(self.pid, "With Manual", [("With Manual.iso", b"game" * 10), ("manual.pdf", b"pdf")])
        self.mock.roms[rid]["files"][1]["category"] = "manual"
        self.dm.enqueue(self.rom(rid))
        self.wait(rid)
        self.assertFalse((self.home / "ROMs" / "ps2" / "manual.pdf").exists())

    def test_a_game_with_no_files_is_refused(self):
        rid = self.mock.add_rom(self.pid, "Empty", [("Empty.iso", b"x")])
        rom = self.rom(rid)
        rom["files"] = []
        with self.assertRaises(ValueError):
            self.dm.enqueue(rom)


class ResumeTests(DlBase):
    def test_a_dropped_connection_resumes_where_it_stopped(self):
        data = self.big()
        rid = self.mock.add_rom(self.pid, "Big", [("Big.iso", data)])
        self.mock.drop_after = 1_000_000
        self.dm.enqueue(self.rom(rid))
        j = self.wait(rid, timeout=40)
        self.assertEqual(j["state"], "done", j)
        self.assertEqual((self.home / "ROMs" / "ps2" / "Big.iso").read_bytes(), data)
        ranges = [r for r in self.mock.requests if "/files/content/" in r[1]]
        self.assertGreaterEqual(len(ranges), 2)                       # asked again after the cut

    def test_a_part_file_from_an_earlier_run_is_continued(self):
        data = self.big(2 * 1024 * 1024)
        rid = self.mock.add_rom(self.pid, "Half", [("Half.iso", data)])
        part = self.home / "ROMs" / "ps2" / "Half.iso.part"
        fixtures.write(str(part), data[:700_000])
        self.dm.enqueue(self.rom(rid))
        self.assertEqual(self.wait(rid)["state"], "done")
        self.assertEqual((self.home / "ROMs" / "ps2" / "Half.iso").read_bytes(), data)

    def test_a_server_that_ignores_ranges_still_gives_a_correct_file(self):
        data = self.big(1024 * 1024)
        rid = self.mock.add_rom(self.pid, "NoRange", [("NoRange.iso", data)])
        fixtures.write(str(self.home / "ROMs" / "ps2" / "NoRange.iso.part"), data[:300_000])
        self.mock.ignore_range = True
        self.dm.enqueue(self.rom(rid))
        self.assertEqual(self.wait(rid)["state"], "done")
        self.assertEqual((self.home / "ROMs" / "ps2" / "NoRange.iso").read_bytes(), data)

    def test_temporary_server_errors_are_retried(self):
        rid = self.mock.add_rom(self.pid, "Flaky", [("Flaky.iso", b"y" * 5000)])
        self.mock.fail_downloads = 1
        self.dm.enqueue(self.rom(rid))
        self.assertEqual(self.wait(rid, timeout=30)["state"], "done")

    def test_a_queue_survives_a_restart(self):
        rid = self.mock.add_rom(self.pid, "Later", [("Later.iso", b"z" * 4000)])
        self.dm.pause(rid)
        self.dm.stop()
        from rommarmada.downloads import DownloadManager
        self.dm.settings.update({"library": {"max_concurrent": 1}})
        # queue a job while stopped by writing through enqueue on a manager that is not started
        mgr = DownloadManager(self.settings, self.manifest, self.client, self.emit)
        mgr.enqueue(self.rom(rid))
        mgr._save()
        again = DownloadManager(self.settings, self.manifest, self.client, self.emit)
        again.start()
        try:
            end = time.time() + 20
            while time.time() < end and (again.get(rid) or {}).get("state") != "done":
                time.sleep(0.05)
            self.assertEqual(again.get(rid)["state"], "done")
        finally:
            again.stop()
        self.dm = again


class SafetyTests(DlBase):
    def test_a_checksum_mismatch_is_caught_and_the_file_not_kept(self):
        rid = self.mock.add_rom(self.pid, "Bad", [("Bad.iso", b"good bytes" * 100)])
        self.mock.roms[rid]["files"][0]["crc_hash"] = "deadbeef"
        self.dm.enqueue(self.rom(rid))
        j = self.wait(rid, timeout=40)
        self.assertEqual(j["state"], "error", j)
        self.assertIn("checksum", j["error"])
        self.assertFalse((self.home / "ROMs" / "ps2" / "Bad.iso").exists())
        self.assertFalse((self.home / "ROMs" / "ps2" / "Bad.iso.part").exists())

    def test_verification_can_be_turned_off(self):
        rid = self.mock.add_rom(self.pid, "Skip", [("Skip.iso", b"bytes" * 100)])
        self.mock.roms[rid]["files"][0]["crc_hash"] = "deadbeef"
        self.settings.update({"library": {"verify": "size"}})
        self.dm.enqueue(self.rom(rid))
        self.assertEqual(self.wait(rid)["state"], "done")

    def test_not_enough_space_is_refused_before_downloading(self):
        rid = self.mock.add_rom(self.pid, "Huge", [("Huge.iso", b"h" * 5000)])
        self.mock.roms[rid]["files"][0]["file_size_bytes"] = 10 ** 15
        self.dm.enqueue(self.rom(rid))
        j = self.wait(rid)
        self.assertEqual((j["state"], j["error_kind"]), ("error", "disk"))
        self.assertIn("free", j["error"])
        self.assertEqual([r for r in self.mock.requests if "/files/content/" in r[1]], [])

    def test_a_path_that_leaves_the_library_is_refused(self):
        rid = self.mock.add_rom(self.pid, "Evil", [("Evil.iso", b"x" * 100)])
        rom = self.rom(rid)
        self.dm.enqueue(rom)
        self.wait(rid)
        job = self.dm.jobs[rid]
        job.files[0].dest = str(self.tmp / "outside.iso")
        job.state, job.error = "queued", ""
        self.dm.pause(rid); self.dm.resume(rid)
        j = self.wait(rid)
        self.assertEqual(j["state"], "error")
        self.assertFalse((self.tmp / "outside.iso").exists())

    def test_cancel_removes_the_partial_file(self):
        data = self.big(4 * 1024 * 1024)
        rid = self.mock.add_rom(self.pid, "Cancelled", [("Cancelled.iso", data)])
        self.settings.update({"library": {"speed_limit_kbs": 1500}})
        self.dm.enqueue(self.rom(rid))
        end = time.time() + 10
        while time.time() < end and (self.dm.get(rid)["done"] < 200_000):
            time.sleep(0.05)
        self.dm.cancel(rid)
        j = self.wait(rid, ("cancelled",))
        self.assertEqual(j["state"], "cancelled")
        self.assertFalse((self.home / "ROMs" / "ps2" / "Cancelled.iso.part").exists())

    def test_pause_keeps_the_part_and_resume_finishes(self):
        data = self.big(4 * 1024 * 1024)
        rid = self.mock.add_rom(self.pid, "Paused", [("Paused.iso", data)])
        self.settings.update({"library": {"speed_limit_kbs": 1500}})
        self.dm.enqueue(self.rom(rid))
        end = time.time() + 10
        while time.time() < end and (self.dm.get(rid)["done"] < 200_000):
            time.sleep(0.05)
        self.dm.pause(rid)
        self.assertEqual(self.wait(rid, ("paused",))["state"], "paused")
        part = self.home / "ROMs" / "ps2" / "Paused.iso.part"
        self.assertTrue(part.exists())
        self.settings.update({"library": {"speed_limit_kbs": 0}})
        self.dm.resume(rid)
        self.assertEqual(self.wait(rid, ("done", "error"), timeout=30)["state"], "done")
        self.assertEqual((self.home / "ROMs" / "ps2" / "Paused.iso").read_bytes(), data)

    def test_an_auth_failure_is_reported_as_one(self):
        rid = self.mock.add_rom(self.pid, "Locked", [("Locked.iso", b"x" * 100)])
        self.settings.update({"auth": {"token": "rmm_revoked"}})
        self.dm.enqueue(self.rom_with(rid))
        j = self.wait(rid)
        self.assertEqual((j["state"], j["error_kind"]), ("error", "auth"))

    def rom_with(self, rid):
        return self.mock.roms[rid]


class UninstallTests(DlBase):
    def test_uninstall_deletes_the_games_files_and_nothing_else(self):
        rid = self.mock.add_rom(self.pid, "Gone", [("Gone.m3u", b"a"), ("a.chd", b"b" * 50)])
        self.dm.enqueue(self.rom(rid))
        self.wait(rid)
        other = self.home / "ROMs" / "ps2" / "Keep.iso"
        fixtures.write(str(other), b"mine")
        card = self.home / ".config" / "ARMSX2" / "memcards" / "Mcd001.ps2" / "BASLUS-1" / "x"
        fixtures.write(str(card), b"save")
        self.dm.uninstall(rid)
        self.assertFalse((self.home / "ROMs" / "ps2" / "Gone").exists())
        self.assertTrue(other.exists())
        self.assertTrue(card.exists())
        self.assertIsNone(self.manifest.get(rid))

    def test_zip_extraction_when_asked(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("Zipped.iso", b"inside" * 100)
        rid = self.mock.add_rom(self.pid, "Zipped", [("Zipped.zip", buf.getvalue())])
        self.settings.update({"library": {"extract_zip": True}})
        self.dm.enqueue(self.rom(rid))
        self.assertEqual(self.wait(rid)["state"], "done")
        self.assertEqual((self.home / "ROMs" / "ps2" / "Zipped.iso").read_bytes(), b"inside" * 100)
        self.assertFalse((self.home / "ROMs" / "ps2" / "Zipped.zip").exists())
        self.assertTrue(self.manifest.get(rid)["main_file"].endswith("Zipped.iso"))
