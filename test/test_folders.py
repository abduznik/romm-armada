# SPDX-License-Identifier: MIT
"""Saves as a zip of the game's save folders (what Argosy and Freegosy upload to a RomM before 5.5), pulled from any slot."""
import io
import json
import os
import shutil
import tempfile
import time
import unittest
import zipfile
from datetime import timedelta
from pathlib import Path

import fixtures
from mock_romm import romm_hash
from support import BIN, Device, Env


def zip_of(entries: dict) -> bytes:
    """A zip from {name: bytes}; a name ending in / is a directory entry."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in entries.items():
            if name.endswith("/"):
                z.writestr(zipfile.ZipInfo(name), b"")
            else:
                z.writestr(name, data)
    return buf.getvalue()


def freegosy_zip(folder="BASLUS-20152AC04", files=None) -> bytes:
    """How Freegosy bundles a PCSX2 save: its sync note, then the game's folder at the root of the archive."""
    files = files or {"icon.sys": b"PS2D" + b"\0" * 60 + b"title", "save.dat": bytes(range(256)) * 4}
    entries = {"freegosy_sync.txt": json.dumps({"contentHash": "0123456789abcdef"}).encode(), f"{folder}/": b""}
    entries.update({f"{folder}/{n}": d for n, d in files.items()})
    return zip_of(entries)


class SavefolderUnitTests(unittest.TestCase):
    def setUp(self):
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "py_modules"))
        self.tmp = Path(tempfile.mkdtemp(prefix="savefolders-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def card(self, **folders):
        card = self.tmp / "Mcd001.ps2"
        card.mkdir(exist_ok=True)
        (card / "_pcsx2_superblock").write_bytes(b"\xff" * 8192)
        for name, files in folders.items():
            (card / name).mkdir()
            for fname, data in files.items():
                (card / name / fname).write_bytes(data)
        return card

    def test_which_folders_are_a_games(self):
        from rommarmada.savefolders import folder_matches as m
        self.assertTrue(m("BASLUS-20152", "BASLUS-20152"))
        self.assertTrue(m("BASLUS-20152AC04", "BASLUS-20152"))
        self.assertTrue(m("BASLUS-20642SYS", "BASLUS-20642"))
        self.assertTrue(m("BASLUS-20642RD0", "BASLUS-20642"))
        self.assertTrue(m("basl_us-20152x", "BASLUS-20152"), "case and punctuation do not matter")
        self.assertTrue(m("BESLUS-20152", "BASLUS-20152"), "a wrong region letter still finds it")
        self.assertTrue(m("BASLUS-21269BULLY000", "SLUS-21269"), "a bare serial gets its region prefix")
        self.assertTrue(m("BESLES-50000", "SLES-50000"))
        self.assertTrue(m("BISLPS-25000", "SLPS-25000"))
        self.assertFalse(m("BASLUS-20153", "BASLUS-20152"))
        self.assertFalse(m("BESLES-99999", "BASLUS-20152"))
        self.assertFalse(m("anything", ""))

    def test_folders_are_found_across_cards_and_junk_is_left_out(self):
        from rommarmada.savefolders import find_folders
        c1 = self.card(**{"BASLUS-20152AC04": {"a": b"1"}, "BESLES-99999": {"a": b"1"}})
        (c1 / ".hidden").mkdir()
        (c1 / "BASLUS-20152file").write_bytes(b"a file is not a save folder")
        (c1 / "BASLUS-20152link").symlink_to(self.tmp)
        c2 = self.tmp / "Mcd002.ps2"
        (c2 / "BASLUS-20152AC04").mkdir(parents=True)          # the same name on the second card: the first is used
        (c2 / "BASLUS-20152SYS").mkdir()
        found = find_folders([c1, c2], "BASLUS-20152")
        self.assertEqual([f.name for f in found], ["BASLUS-20152AC04", "BASLUS-20152SYS"])
        self.assertEqual(found[0].parent, c1)

    def test_the_upload_is_a_zip_of_the_folders_and_its_hash_is_romms(self):
        from rommarmada.savefolders import build
        card = self.card(**{"BASLUS-20152AC04": {"icon.sys": b"icon", "save.dat": os.urandom(5000)},
                            "BASLUS-20152SYS": {"icon.sys": b"icon2"}})
        out = self.tmp / "u.zip"
        built = build([card / "BASLUS-20152AC04", card / "BASLUS-20152SYS"], out)
        with zipfile.ZipFile(out) as z:
            self.assertEqual(sorted(z.namelist()), ["BASLUS-20152AC04/", "BASLUS-20152AC04/icon.sys", "BASLUS-20152AC04/save.dat",
                                                    "BASLUS-20152SYS/", "BASLUS-20152SYS/icon.sys"])
            self.assertIsNone(z.testzip())
        self.assertEqual(built.content_hash, romm_hash(out.read_bytes()), "the hash RomM will give the upload")
        self.assertEqual((built.files, built.folders), (3, ["BASLUS-20152AC04", "BASLUS-20152SYS"]))
        self.assertLess(built.size, built.data_bytes + 2000)

    def test_the_hash_depends_on_the_content_and_not_on_dates_or_zipping(self):
        from rommarmada.savefolders import build, romm_content_hash
        card = self.card(**{"BASLUS-20152": {"icon.sys": b"icon", "save.dat": b"one"}})
        a = build([card / "BASLUS-20152"], self.tmp / "a.zip")
        os.utime(card / "BASLUS-20152" / "save.dat", (1_000_000_000, 1_000_000_000))
        b = build([card / "BASLUS-20152"], self.tmp / "b.zip")
        self.assertEqual(a.content_hash, b.content_hash)
        (card / "BASLUS-20152" / "save.dat").write_bytes(b"two")
        self.assertNotEqual(a.content_hash, build([card / "BASLUS-20152"], self.tmp / "c.zip").content_hash)
        # the same files zipped by someone else, with another compression and date, hash the same
        other = zip_of({"BASLUS-20152/": b"", "BASLUS-20152/icon.sys": b"icon", "BASLUS-20152/save.dat": b"one"})
        self.assertEqual(romm_content_hash(other), a.content_hash)
        # not a zip: the md5 of the bytes, as RomM does
        import hashlib
        self.assertEqual(romm_content_hash(b"\x00" * 100), hashlib.md5(b"\x00" * 100).hexdigest())

    def test_nothing_to_build_from(self):
        from rommarmada.savefolders import ArchiveError, NoFiles, build
        card = self.card(**{"BASLUS-20152": {}})
        with self.assertRaises(NoFiles):
            build([card / "BASLUS-20152"], self.tmp / "e.zip")
        self.assertFalse((self.tmp / "e.zip").exists())
        self.assertTrue(issubclass(NoFiles, ArchiveError))

    def restore(self, data: bytes, card: Path, save_id="BASLUS-20152"):
        from rommarmada import savefolders as sf
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            pl = sf.plan(zf, save_id)
            return pl, sf.restore(zf, pl, card, save_id, self.tmp / "scratch")

    def test_the_shapes_in_use_are_all_read(self):
        files = {"icon.sys": b"i", "save.dat": b"s"}
        shapes = {
            "the game's folders at the root (Argosy, Freegosy, this plugin)": zip_of(
                {"BASLUS-20152AC04/": b"", "BASLUS-20152AC04/icon.sys": b"i", "BASLUS-20152AC04/save.dat": b"s"}),
            "Freegosy, with its sync note": freegosy_zip("BASLUS-20152AC04", files),
            "a whole folder card with a superblock, whatever it is called": zip_of(
                {"test/_pcsx2_superblock": b"x", "test/BASLUS-20152AC04/icon.sys": b"i", "test/BASLUS-20152AC04/save.dat": b"s",
                 "test/BESLES-99999/icon.sys": b"other game"}),
            "a card folder called Mcd001.ps2": zip_of(
                {"Mcd001.ps2/BASLUS-20152AC04/icon.sys": b"i", "Mcd001.ps2/BASLUS-20152AC04/save.dat": b"s",
                 "Mcd001.ps2/BESLES-99999/icon.sys": b"other game"}),
            "a wrong region letter": zip_of({"BESLUS-20152AC04/icon.sys": b"i", "BESLUS-20152AC04/save.dat": b"s"}),
        }
        for what, data in shapes.items():
            with self.subTest(what):
                card = self.card()
                self.restore(data, card)
                names = sorted(p.name for p in card.iterdir())
                self.assertNotIn("BESLES-99999", names, "another game's folder is never written")
                self.assertNotIn("freegosy_sync.txt", names)
                folder = next(p for p in card.iterdir() if p.is_dir())
                self.assertEqual({p.name: p.read_bytes() for p in folder.iterdir()}, files)
                shutil.rmtree(card)

    def test_restoring_replaces_the_games_folders_and_only_those(self):
        card = self.card(**{"BASLUS-20152AC04": {"old.dat": b"old", "icon.sys": b"old icon"},
                            "BASLUS-20152OLD": {"x": b"a folder the archive lacks"},
                            "BESLES-99999": {"mine": b"another game, untouched"}})
        pl, done = self.restore(freegosy_zip("BASLUS-20152AC04"), card)
        self.assertEqual(done, ["BASLUS-20152AC04"])
        self.assertEqual(sorted(p.name for p in (card / "BASLUS-20152AC04").iterdir()), ["icon.sys", "save.dat"], "no leftover file")
        self.assertFalse((card / "BASLUS-20152OLD").exists(), "a folder of this game that the archive lacks is dropped")
        self.assertEqual((card / "BESLES-99999" / "mine").read_bytes(), b"another game, untouched")
        self.assertTrue((card / "_pcsx2_superblock").is_file())
        self.assertFalse((self.tmp / "scratch").exists(), "the work folder is cleaned up")
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir() if p.name.startswith(".")), [])

    def test_dates_from_the_archive_are_kept(self):
        card = self.card()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr(zipfile.ZipInfo("BASLUS-20152/save.dat", date_time=(2024, 5, 6, 7, 8, 10)), b"data")
        self.restore(buf.getvalue(), card)
        got = time.localtime((card / "BASLUS-20152" / "save.dat").stat().st_mtime)[:6]
        self.assertEqual(got, (2024, 5, 6, 7, 8, 10))

    def test_unsafe_archives_are_refused_before_anything_is_written(self):
        from rommarmada.savefolders import ArchiveError
        card = self.card(**{"BASLUS-20152": {"keep": b"mine"}})
        bad = {
            "a path that climbs out": zip_of({"BASLUS-20152/../../evil": b"x", "BASLUS-20152/save.dat": b"s"}),
            "an absolute path": zip_of({"/etc/evil": b"x", "BASLUS-20152/save.dat": b"s"}),
            "backslashes that climb": zip_of({"BASLUS-20152\\..\\..\\evil": b"x"}),
            "a drive letter": zip_of({"C:/evil": b"x", "BASLUS-20152/save.dat": b"s"}),
        }
        for what, data in bad.items():
            with self.subTest(what):
                with self.assertRaises(ArchiveError):
                    self.restore(data, card)
                self.assertEqual((card / "BASLUS-20152" / "keep").read_bytes(), b"mine")
        link = io.BytesIO()
        with zipfile.ZipFile(link, "w") as z:
            info = zipfile.ZipInfo("BASLUS-20152/link")
            info.external_attr = 0o120777 << 16
            z.writestr(info, "/etc/passwd")
        with self.assertRaises(ArchiveError):
            self.restore(link.getvalue(), card)
        self.assertFalse((self.tmp / "evil").exists())

    def test_an_archive_with_nothing_of_this_game_is_refused(self):
        from rommarmada.savefolders import ArchiveError
        card = self.card(**{"BASLUS-20152": {"keep": b"mine"}})
        with self.assertRaises(ArchiveError) as cm:
            self.restore(zip_of({"BESLES-99999/icon.sys": b"x"}), card)
        self.assertIn("nothing for this game", str(cm.exception))
        with self.assertRaises(ArchiveError):
            self.restore(zip_of({"readme.txt": b"hello"}), card)
        self.assertEqual((card / "BASLUS-20152" / "keep").read_bytes(), b"mine")

    def test_old_bundles_with_big_save_states_beside_a_small_save_are_fine(self):
        card = self.card()
        data = zip_of({"freegosy_sync.txt": b"{}", "BASLUS-20152AC04/icon.sys": b"i", "BASLUS-20152AC04/save.dat": b"s",
                       "Test Game (SLUS-20152).01.p2s": b"\0" * (70 * 1024 * 1024)})
        self.restore(data, card)
        self.assertEqual(sorted(p.name for p in (card / "BASLUS-20152AC04").iterdir()), ["icon.sys", "save.dat"])
        self.assertEqual([p.name for p in card.iterdir() if p.name.endswith(".p2s")], [])
        from rommarmada.savefolders import ArchiveError
        huge = zip_of({"BASLUS-20152AC04/save.dat": b"\0" * (70 * 1024 * 1024)})
        with self.assertRaises(ArchiveError):
            self.restore(huge, card)

    def test_a_memory_card_image_inside_a_zip_is_recognised(self):
        from rommarmada import savefolders as sf
        data = zip_of({"freegosy_sync.txt": b"{}", "Mcd001.ps2": b"\xff" * (8 * 1024 * 1024)})
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            pl = sf.plan(zf, "BASLUS-20152")
        self.assertEqual((pl.shape, [i.filename for i in pl.images]), ("image", ["Mcd001.ps2"]))

    def test_a_failure_part_way_puts_the_old_folders_back(self):
        from rommarmada import savefolders as sf
        card = self.card(**{"BASLUS-20152AC04": {"icon.sys": b"old"}, "BASLUS-20152SYS": {"icon.sys": b"old sys"}})
        data = zip_of({"BASLUS-20152AC04/icon.sys": b"new", "BASLUS-20152SYS/icon.sys": b"new sys"})
        real = os.replace
        calls = {"n": 0}

        def flaky(src, dst):
            if str(dst).startswith(str(card)):
                calls["n"] += 1
                if calls["n"] == 2:           # the second folder cannot be put in place
                    raise OSError("disk went away")
            return real(src, dst)
        sf.os.replace = flaky
        try:
            with self.assertRaises(OSError):
                self.restore(data, card)
        finally:
            sf.os.replace = real
        self.assertEqual((card / "BASLUS-20152AC04" / "icon.sys").read_bytes(), b"old")
        self.assertEqual((card / "BASLUS-20152SYS" / "icon.sys").read_bytes(), b"old sys")


class FolderSync(Env):
    """Two handhelds on the defaults: save folders in the `armadaos` slot, on a server like the user's (RomM 5.0)."""

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

    def server_saves(self, slot="armadaos"):
        return sorted(self.mock.saves_of(self.rom, slot), key=lambda s: s["updated_at"], reverse=True)       # newest first

    def record(self, dev):
        from rommarmada import saves
        with dev.active():
            return saves.Record(self.rom)

    def files_on(self, dev, folder="BASLUS-20152"):
        d = dev.card() / folder
        return {p.name: p.read_bytes() for p in d.iterdir()} if d.is_dir() else {}


class UploadTests(FolderSync):
    def test_the_upload_is_a_few_kb_zip_of_this_games_folder_tagged_armadaos(self):
        self.a.play(b"chapter 1" * 100)
        self.a.play(b"someone else's", serial_folder="BESLES-99999")
        r = self.sync(self.a)
        self.assertEqual(r["status"], "uploaded", r)
        (save,) = self.server_saves()
        self.assertEqual((save["slot"], save["emulator"]), ("armadaos", "armadaos"))
        self.assertRegex(save["file_name"], r"^Test Game \(USA\) \[.*\]\.zip$")
        z = zipfile.ZipFile(io.BytesIO(save["data"]))
        self.assertIn("BASLUS-20152/save.dat", z.namelist())
        self.assertFalse([n for n in z.namelist() if "BESLES" in n or "superblock" in n], "only this game's folder")
        self.assertLess(len(save["data"]), 20_000)
        rec = self.record(self.a).last
        self.assertEqual(rec["format"], "folders")
        self.assertEqual(rec["local_hash"], save["content_hash"], "the plugin and RomM hash the zip the same way")
        self.assertEqual(rec["server_hash"], save["content_hash"])
        self.assertEqual(rec["unit"]["kind"], "folders")
        self.assertEqual(rec["unit"]["contents"], ["BASLUS-20152"])

    def test_a_game_with_several_save_folders_sends_them_all(self):
        self.a.play(b"data" * 50, serial_folder="BASLUS-20152AC04")
        self.a.play(b"system" * 20, serial_folder="BASLUS-20152SYS")
        self.assertEqual(self.sync(self.a)["status"], "uploaded")
        names = zipfile.ZipFile(io.BytesIO(self.server_saves()[0]["data"])).namelist()
        self.assertIn("BASLUS-20152AC04/save.dat", names)
        self.assertIn("BASLUS-20152SYS/save.dat", names)

    def test_nothing_to_do_then_upload_after_a_change(self):
        self.a.play(b"v1" * 100)
        self.assertEqual(self.sync(self.a)["status"], "uploaded")
        self.assertEqual(self.sync(self.a)["status"], "up_to_date")
        self.assertEqual(len(self.server_saves()), 1)
        time.sleep(0.02)
        self.a.play(b"v2" * 100)
        self.assertEqual(self.sync(self.a)["status"], "uploaded")
        self.assertEqual(len(self.server_saves()), 2)
        with self.a.active():
            r = self.a.sync.sync([self.rom], only_if_changed=True)[0]
        self.assertEqual(r["status"], "up_to_date")

    def test_no_saves_no_upload(self):
        self.assertEqual(self.sync(self.a)["status"], "no_saves")
        self.assertEqual(self.mock.saves, [])


class PullTests(FolderSync):
    def test_a_second_handheld_gets_the_folders_and_is_then_in_sync(self):
        self.a.play(b"from handheld a" * 40)
        self.sync(self.a)
        r = self.sync(self.b)
        self.assertEqual(r["status"], "downloaded", r)
        self.assertEqual(self.files_on(self.b), self.files_on(self.a))
        self.assertTrue((self.b.card() / "_pcsx2_superblock").is_file(), "the card was made a card the emulator opens")
        self.assertEqual(self.sync(self.b)["status"], "up_to_date")
        self.assertEqual(len(self.server_saves()), 1, "nothing new was uploaded")
        rec = self.record(self.b).last
        self.assertEqual((rec["action"], rec["format"], rec["local_hash"]), ("download", "folders", self.server_saves()[0]["content_hash"]))

    def test_a_card_is_backed_up_before_it_is_written(self):
        self.a.play(b"v1" * 100)
        self.sync(self.a)
        self.b.play(b"b's own" * 10, serial_folder="BESLES-99999")
        self.sync(self.b)
        with self.b.active():
            from rommarmada import saves
            backups = saves.list_backups(self.rom)
        self.assertEqual(len(backups), 1)
        self.assertTrue((Path(backups[0]["path"]) / "Mcd001.ps2" / "BESLES-99999" / "save.dat").is_file())
        self.assertTrue((self.b.card() / "BESLES-99999" / "save.dat").is_file(), "the other game's save is still there")

    def test_a_newer_save_from_the_other_handheld_replaces_an_unchanged_one(self):
        self.a.play(b"v1" * 100)
        self.sync(self.a)
        self.sync(self.b)
        time.sleep(0.02)
        self.a.play(b"v2 is longer" * 100)
        self.sync(self.a)
        self.assertEqual(self.sync(self.b)["status"], "downloaded")
        self.assertEqual(self.files_on(self.b)["save.dat"], b"v2 is longer" * 100)

    def test_both_changed_asks_and_changes_nothing(self):
        self.a.play(b"v1" * 100)
        self.sync(self.a)
        self.sync(self.b)
        time.sleep(0.02)
        self.a.play(b"a's v2" * 100)
        self.sync(self.a)
        self.b.play(b"b's v2" * 100)
        r = self.sync(self.b)
        self.assertEqual(r["status"], "conflict", r)
        self.assertEqual(self.files_on(self.b)["save.dat"], b"b's v2" * 100)
        r = self.sync(self.b, resolutions={self.rom: "server"})
        self.assertEqual(r["status"], "downloaded", r)
        self.assertEqual(self.files_on(self.b)["save.dat"], b"a's v2" * 100)

    def test_a_device_that_never_synced_does_not_overwrite_on_a_guess(self):
        self.a.play(b"a's game" * 50)
        self.sync(self.a)
        self.b.play(b"b's own game" * 50)
        r = self.sync(self.b)
        self.assertEqual((r["status"], r["conflict"]["reason"]), ("conflict", "first_contact"))
        self.assertEqual(self.files_on(self.b)["save.dat"], b"b's own game" * 50)

    def test_the_emulator_running_blocks_both_directions(self):
        self.a.play(b"hello" * 100)
        self.sync(self.a)
        proc = self.tmp / "proc"
        d = proc / "77"
        d.mkdir(parents=True)
        (d / "comm").write_text("armsx2-qt\n")
        (d / "cmdline").write_bytes(b"armsx2-qt\0")
        with self.b.active():
            self.b.engine(proc_root=str(proc))
        self.assertEqual(self.sync(self.b)["status"], "busy")
        self.assertFalse(self.b.card().exists())


class FormatTests(FolderSync):
    def test_the_server_decides_like_argosy_does(self):
        self.a.play(b"hello" * 100)
        self.assertEqual(self.sync(self.a)["status"], "uploaded")
        self.assertTrue(zipfile.is_zipfile(io.BytesIO(self.server_saves()[0]["data"])), "RomM 5.0 has no SAVE_SYNC: save folders")
        self.mock.snapshots = False
        self.a.sync._caps = None
        time.sleep(0.02)
        self.a.play(b"two" * 100)
        self.sync(self.a)
        self.assertTrue(zipfile.is_zipfile(io.BytesIO(self.server_saves()[0]["data"])), "RomM says it has no snapshots: save folders")
        self.mock.snapshots = True            # RomM 5.5 and newer
        self.a.sync._caps = None
        time.sleep(0.02)
        self.a.play(b"three" * 100)
        self.sync(self.a)
        newest = self.server_saves()[0]
        self.assertEqual(len(newest["data"]), 8650752, "a server that keeps units gets sigil's card image")
        self.assertEqual(self.record(self.a).last["format"], "card")

    def test_the_setting_can_force_either_format(self):
        self.mock.snapshots = True
        self.a.settings.update({"saves": {"format": "folders"}})
        self.a.play(b"hello" * 100)
        self.sync(self.a)
        self.assertTrue(zipfile.is_zipfile(io.BytesIO(self.server_saves()[0]["data"])))
        self.mock.snapshots = None
        self.b.settings.update({"saves": {"format": "card"}})
        self.b.play(b"other" * 100, serial_folder="BASLUS-20152")
        self.sync(self.b)          # b has no history: this is a first contact with a different save, so it asks first
        self.sync(self.b, resolutions={self.rom: "device"})
        self.assertEqual(len(self.server_saves()[0]["data"]), 8650752)

    def test_an_offline_sync_decides_like_the_last_time(self):
        self.mock.snapshots = True
        with self.a.active():
            self.assertTrue(self.a.sync._server_units())
            self.a.sync._caps = None
            self.mock.stop()
            self.assertTrue(self.a.sync._server_units(), "remembered from the last time the server answered")

    def test_a_server_never_seen_means_save_folders(self):
        self.mock.stop()
        with self.a.active():
            self.assertFalse(self.a.sync._server_units())

    def test_changing_the_format_uploads_the_same_save_once_more_in_the_new_one(self):
        self.a.settings.update({"saves": {"format": "card"}})
        self.a.play(b"hello" * 100)
        self.assertEqual(self.sync(self.a)["status"], "uploaded")
        self.assertEqual(len(self.server_saves()), 1)
        self.a.settings.update({"saves": {"format": "folders"}})
        self.assertEqual(self.sync(self.a)["status"], "uploaded")
        self.assertEqual(len(self.server_saves()), 2)
        self.assertEqual(self.record(self.a).last["format"], "folders")
        self.assertEqual(self.sync(self.a)["status"], "up_to_date")
        self.assertEqual(len(self.server_saves()), 2)

    def test_a_file_card_under_the_auto_policy_uses_the_card_image(self):
        self.a.play(b"hello" * 100)
        self.sync(self.a)
        with self.b.active():
            sb = self.savebridge()
            unit = fixtures.write(str(self.tmp / "u.zip"), fixtures.make_save_zip("BESLES-77777"))
            root = str(self.b.home / ".config" / "ARMSX2")
            os.makedirs(root, exist_ok=True)
            sb.restore(layout="pcsx2_standalone", root=root, unit=Path(unit), rom=None, platform_slug="ps2", content="/x/Other.iso",
                       identity={"platform": "ps2", "title_id": "SLES-77777", "save_id": "BESLES-77777"})
        self.b.settings.update({"saves": {"card_type": "auto"}})
        r = self.sync(self.b)
        self.assertEqual(r["status"], "downloaded", r)
        self.assertTrue(self.b.card().is_file(), "the file card the user chose is still a file card")
        self.assertEqual(self.record(self.b).last["format"], "card")

    def test_forcing_save_folders_on_a_file_card_says_why_it_cannot(self):
        with self.b.active():
            sb = self.savebridge()
            unit = fixtures.write(str(self.tmp / "u.zip"), fixtures.make_save_zip("BASLUS-20152"))
            root = str(self.b.home / ".config" / "ARMSX2")
            os.makedirs(root, exist_ok=True)
            sb.restore(layout="pcsx2_standalone", root=root, unit=Path(unit), rom=None, platform_slug="ps2", content="/x/Test.iso",
                       identity={"platform": "ps2", "title_id": "SLUS-20152", "save_id": "BASLUS-20152"})
        self.b.settings.update({"saves": {"card_type": "auto", "format": "folders"}})
        r = self.sync(self.b)
        self.assertEqual(r["status"], "error", r)
        self.assertIn("folder memory card", r["message"])


class AnySlotTests(FolderSync):
    """Freegosy's saves are in its own slot, Argosy's in `autosave`: the latest is pulled from wherever it is."""

    def seed_freegosy(self, data=None, age=timedelta(hours=1), slot="freegosy", name="Test Game (USA).zip", emulator="freegosy"):
        return self.mock.seed_save(self.rom, data or freegosy_zip(), slot=slot, emulator=emulator, age=age, file_name=name)

    def test_a_fresh_install_downloads_the_freegosy_save(self):
        self.seed_freegosy()
        r = self.sync(self.a)
        self.assertEqual(r["status"], "downloaded", r)
        self.assertEqual(sorted(self.files_on(self.a, "BASLUS-20152AC04")), ["icon.sys", "save.dat"])
        self.assertFalse(any(p.name == "freegosy_sync.txt" for p in self.a.card().rglob("*")), "its sync note is not a save file")
        rec = self.record(self.a).last
        self.assertEqual((rec["action"], rec["slot"]), ("download", "freegosy"))
        self.assertEqual(self.sync(self.a)["status"], "up_to_date")
        self.assertEqual(self.server_saves(), [], "nothing was written to this device's own slot")

    def test_then_playing_uploads_to_its_own_slot_and_leaves_the_other_alone(self):
        self.seed_freegosy()
        self.sync(self.a)
        time.sleep(0.02)
        self.a.play(b"played on the handheld" * 20, serial_folder="BASLUS-20152AC04", file="save.dat")
        r = self.sync(self.a)
        self.assertEqual(r["status"], "uploaded", r)
        self.assertEqual(len(self.server_saves()), 1)
        self.assertEqual(len(self.mock.saves_of(self.rom, "freegosy")), 1)
        self.assertEqual(self.sync(self.a)["status"], "up_to_date")

    def test_a_newer_save_from_the_pc_is_pulled_and_an_older_one_is_not(self):
        self.seed_freegosy(age=timedelta(hours=2))
        self.sync(self.a)
        time.sleep(0.02)
        self.a.play(b"handheld" * 20, serial_folder="BASLUS-20152AC04", file="save.dat")
        self.sync(self.a)
        # played on the PC afterwards: newer than anything this device has
        newer = freegosy_zip(files={"icon.sys": b"PS2D" + b"\0" * 60, "save.dat": b"pc's later save" * 30})
        self.seed_freegosy(newer, age=timedelta(seconds=-60))
        r = self.sync(self.a)
        self.assertEqual(r["status"], "downloaded", r)
        self.assertEqual(self.files_on(self.a, "BASLUS-20152AC04")["save.dat"], b"pc's later save" * 30)
        # an older one turning up later (another PC, uploaded late) is not a reason to go back in time
        self.seed_freegosy(freegosy_zip(files={"icon.sys": b"x", "save.dat": b"ancient"}), age=timedelta(days=5))
        self.assertEqual(self.sync(self.a)["status"], "up_to_date")
        self.assertEqual(self.files_on(self.a, "BASLUS-20152AC04")["save.dat"], b"pc's later save" * 30)

    def test_the_older_head_of_this_devices_own_slot_cannot_pull_it_back(self):
        old_own = self.mock.seed_save(self.rom, freegosy_zip(files={"icon.sys": b"x", "save.dat": b"old own"}), slot="armadaos",
                                      emulator="armadaos", age=timedelta(hours=2), file_name="Test Game (USA).zip")
        self.seed_freegosy(freegosy_zip(files={"icon.sys": b"x", "save.dat": b"newer freegosy"}), age=timedelta(hours=1))
        self.assertEqual(self.sync(self.a)["status"], "downloaded")
        self.assertEqual(self.files_on(self.a, "BASLUS-20152AC04")["save.dat"], b"newer freegosy")
        self.assertEqual(self.sync(self.a)["status"], "up_to_date")
        self.assertEqual(self.files_on(self.a, "BASLUS-20152AC04")["save.dat"], b"newer freegosy")
        self.assertEqual(old_own["slot"], "armadaos")
        # playing on from it uploads over that older head, which RomM would refuse a device that never synced it
        time.sleep(0.02)
        self.a.play(b"played on from freegosy's" * 10, serial_folder="BASLUS-20152AC04", file="save.dat")
        r = self.sync(self.a)
        self.assertEqual(r["status"], "uploaded", r)
        self.assertEqual(self.server_saves()[0]["id"] != old_own["id"], True)
        self.assertEqual(self.sync(self.a)["status"], "up_to_date")

    def test_a_slot_head_newer_than_anything_taken_is_still_a_real_conflict(self):
        """Another device's newer save in this device's slot, never taken here: uploading over it is for the user to decide."""
        self.a.play(b"first" * 40)
        self.sync(self.a)
        self.mock.seed_save(self.rom, freegosy_zip(files={"icon.sys": b"x", "save.dat": b"b's later save"}), slot="armadaos",
                            emulator="armadaos", age=timedelta(seconds=-30), file_name="Test Game (USA).zip")
        time.sleep(0.02)
        self.a.play(b"second, played here" * 40)
        self.assertEqual(self.sync(self.a)["status"], "conflict")
        self.assertEqual(self.files_on(self.a)["save.dat"], b"second, played here" * 40)

    def test_the_newest_of_several_other_slots_wins(self):
        self.seed_freegosy(freegosy_zip(files={"icon.sys": b"x", "save.dat": b"older"}), age=timedelta(hours=3))
        self.seed_freegosy(freegosy_zip(files={"icon.sys": b"x", "save.dat": b"newest, from argosy"}), age=timedelta(hours=1),
                           slot="autosave", emulator="armsx2_refresh")
        self.assertEqual(self.sync(self.a)["status"], "downloaded")
        self.assertEqual(self.files_on(self.a, "BASLUS-20152AC04")["save.dat"], b"newest, from argosy")

    def test_a_save_this_device_has_played_since_is_never_replaced_without_asking(self):
        self.seed_freegosy()
        self.a.play(b"already played here" * 20)
        r = self.sync(self.a)
        self.assertEqual((r["status"], r["conflict"]["reason"], r["conflict"]["server"]["slot"]), ("conflict", "first_contact", "freegosy"))
        self.assertEqual(self.files_on(self.a)["save.dat"], b"already played here" * 20)
        # keeping this device's save uploads it to its own slot; the older PC save is not offered again
        r = self.sync(self.a, resolutions={self.rom: "device"})
        self.assertEqual(r["status"], "uploaded", r)
        self.assertEqual(len(self.server_saves()), 1)
        self.assertEqual(self.sync(self.a)["status"], "up_to_date")

    def test_choosing_the_server_save_restores_it_from_its_slot(self):
        self.seed_freegosy()
        self.a.play(b"already played here" * 20)
        self.sync(self.a)
        r = self.sync(self.a, resolutions={self.rom: "server"})
        self.assertEqual(r["status"], "downloaded", r)
        self.assertEqual(sorted(self.files_on(self.a, "BASLUS-20152AC04")), ["icon.sys", "save.dat"])
        self.assertEqual(self.sync(self.a)["status"], "up_to_date")

    def test_after_a_pc_save_a_changed_handheld_save_is_a_conflict_not_an_overwrite(self):
        self.seed_freegosy(age=timedelta(hours=2))
        self.sync(self.a)
        time.sleep(0.02)
        self.a.play(b"handheld change" * 20, serial_folder="BASLUS-20152AC04", file="save.dat")
        self.seed_freegosy(freegosy_zip(files={"icon.sys": b"x", "save.dat": b"pc change"}), age=timedelta(seconds=-60))
        r = self.sync(self.a)
        self.assertEqual(r["status"], "conflict", r)
        self.assertEqual(self.files_on(self.a, "BASLUS-20152AC04")["save.dat"], b"handheld change" * 20)

    def test_an_old_card_image_from_another_slot_is_restored_through_sigil(self):
        old = Device(self, "old-plugin")
        old.settings.update({"saves": {"format": "card", "slot": "autosave"}})        # what 0.1 and 0.2 did
        with old.active():
            old.install(self.rom)
            old.engine()
            old.play(b"from the old plugin" * 30)
            self.assertEqual(old.sync.sync([self.rom])[0]["status"], "uploaded")
        self.assertEqual(len(self.mock.saves_of(self.rom, "autosave")[0]["data"]), 8650752)
        r = self.sync(self.a)
        self.assertEqual(r["status"], "downloaded", r)
        self.assertEqual(self.files_on(self.a)["save.dat"], b"from the old plugin" * 30)
        self.assertEqual(self.sync(self.a)["status"], "up_to_date")
        self.assertEqual(self.server_saves(), [])

    def test_a_whole_shared_card_uploaded_as_it_is_gives_this_game_only(self):
        """Freegosy once uploaded the entire Mcd00N.ps2 file: every game on it. Only this game's saves come back."""
        with self.b.active():
            sb = self.savebridge()
            root = self.tmp / "pc"
            root.mkdir()
            for serial, save_id in (("SLUS-20152", "BASLUS-20152"), ("SLES-77777", "BESLES-77777")):
                unit = fixtures.write(str(self.tmp / f"{serial}.zip"), fixtures.make_save_zip(save_id, {"icon.sys": b"i", "save.dat": serial.encode() * 40}))
                sb.restore(layout="pcsx2_standalone", root=str(root), unit=Path(unit), rom=None, platform_slug="ps2", content=f"/x/{serial}.iso",
                           identity={"platform": "ps2", "title_id": serial, "save_id": save_id})
        card = (root / "memcards" / "Mcd001.ps2").read_bytes()
        self.mock.seed_save(self.rom, card, slot="freegosy", emulator="freegosy", age=timedelta(hours=1), file_name="Mcd002.ps2")
        r = self.sync(self.a)
        self.assertEqual(r["status"], "downloaded", r)
        self.assertEqual(self.files_on(self.a)["save.dat"], b"SLUS-20152" * 40)
        self.assertFalse((self.a.card() / "BESLES-77777").exists())

    def test_a_whole_card_zipped_is_read_too(self):
        with self.b.active():
            sb = self.savebridge()
            root = self.tmp / "pc"
            root.mkdir()
            unit = fixtures.write(str(self.tmp / "u.zip"), fixtures.make_save_zip("BASLUS-20152", {"icon.sys": b"i", "save.dat": b"zipped card" * 40}))
            sb.restore(layout="pcsx2_standalone", root=str(root), unit=Path(unit), rom=None, platform_slug="ps2", content="/x/T.iso",
                       identity={"platform": "ps2", "title_id": "SLUS-20152", "save_id": "BASLUS-20152"})
        card = (root / "memcards" / "Mcd001.ps2").read_bytes()
        self.seed_freegosy(zip_of({"freegosy_sync.txt": b"{}", "Mcd001.ps2": card}))
        r = self.sync(self.a)
        self.assertEqual(r["status"], "downloaded", r)
        self.assertEqual(self.files_on(self.a)["save.dat"], b"zipped card" * 40)

    def test_a_save_in_no_slot_at_all_is_found_too(self):
        """Uploaded by hand or by an old client: RomM's negotiation never offers these, a look at the game's saves does."""
        self.mock.seed_save(self.rom, freegosy_zip(files={"icon.sys": b"x", "save.dat": b"archival"}), slot=None, emulator="freegosy",
                            age=timedelta(hours=1), file_name="Test Game (USA).zip")
        r = self.sync(self.a)
        self.assertEqual(r["status"], "downloaded", r)
        self.assertEqual(self.files_on(self.a, "BASLUS-20152AC04")["save.dat"], b"archival")
        self.assertIn("no slot", r["message"])
        self.assertEqual(self.sync(self.a)["status"], "up_to_date")
        self.assertEqual(self.server_saves(), [])

    def test_a_save_that_cannot_be_read_is_skipped_once_and_not_forever_blocking(self):
        bad = self.seed_freegosy(zip_of({"BESLES-99999/icon.sys": b"another game's"}), age=timedelta(hours=1))
        r = self.sync(self.a)
        self.assertEqual(r["status"], "error", r)
        self.assertIn("nothing for this game", r["message"])
        self.assertIn(bad["id"], self.record(self.a).doc["unusable"])
        self.assertNotEqual(self.sync(self.a)["status"], "error", "it is skipped now")
        # a readable save made later is taken as usual
        self.seed_freegosy(age=timedelta(seconds=-30))
        self.assertEqual(self.sync(self.a)["status"], "downloaded")

    def test_something_that_is_not_a_save_is_not_downloaded_at_all(self):
        self.seed_freegosy(b"not a save", name="Test Game (USA).state1", slot="freegosy", age=timedelta(minutes=5))
        self.seed_freegosy(b"x" * 100, name="Test Game (USA).p2s", slot="other", age=timedelta(minutes=4))
        r = self.sync(self.a)
        self.assertEqual(r["status"], "no_saves", r)
        self.assertNotIn("/api/saves/", " ".join(p for _, p in self.mock.requests if p.endswith("/content")))

    def test_a_save_from_a_server_that_hashes_by_bytes_is_still_checked(self):
        """Servers before the zip hash fix gave every save the md5 of its bytes; the download check accepts that too."""
        import hashlib
        data = freegosy_zip()
        s = self.seed_freegosy(data)
        s["content_hash"] = hashlib.md5(data).hexdigest()
        self.assertEqual(self.sync(self.a)["status"], "downloaded")

    def test_a_download_that_does_not_match_its_hash_is_refused(self):
        s = self.seed_freegosy()
        s["content_hash"] = "0" * 32
        r = self.sync(self.a)
        self.assertEqual(r["status"], "error", r)
        self.assertIn("checksum", r["message"])
        self.assertFalse((self.a.card() / "BASLUS-20152AC04").exists(), "nothing of it was written")


class MigrationTests(Env):
    def test_the_old_default_slot_moves_to_the_new_name_and_a_chosen_one_stays(self):
        from rommarmada import config
        path = Path(os.environ["ROMM_ARMADA_HOME"]) / "settings" / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": 1, "saves": {"slot": "autosave", "push_on_exit": False}, "server": {"url": "http://x:1"}}))
        s = config.Settings()
        s.load()
        self.assertEqual(s.get("saves.slot"), "armadaos")
        self.assertFalse(s.get("saves.push_on_exit"), "the rest is kept")
        self.assertEqual(json.loads(path.read_text())["version"], 2)
        again = config.Settings()
        again.load()
        self.assertEqual(again.get("saves.slot"), "armadaos")
        # a slot the user chose is theirs, and so is going back to autosave once they are on version 2
        path.write_text(json.dumps({"version": 1, "saves": {"slot": "freegosy"}}))
        s = config.Settings()
        s.load()
        self.assertEqual(s.get("saves.slot"), "freegosy")
        s.update({"saves": {"slot": "autosave"}})
        again = config.Settings()
        again.load()
        self.assertEqual(again.get("saves.slot"), "autosave")


if __name__ == "__main__":
    unittest.main()
