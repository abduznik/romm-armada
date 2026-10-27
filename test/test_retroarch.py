# SPDX-License-Identifier: MIT
"""RetroArch saves: the `<game>.srm` (and `.rtc`) in RetroArch's save folder, synced with RomM."""
import hashlib
import io
import json
import os
import subprocess
import time
import zipfile
from datetime import timedelta
from pathlib import Path

from mock_romm import romm_hash
from support import BIN, Device, Env

STEM = "Test Game (USA)"


def cfg_text(**values) -> str:
    return "".join(f'{k} = "{v}"\n' for k, v in values.items())


def flat_zip(entries: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return buf.getvalue()


class ConfigTests(Env):
    def data_dir(self, **cfg):
        d = self.home / ".config" / "retroarch"
        d.mkdir(parents=True, exist_ok=True)
        (d / "retroarch.cfg").write_text(cfg_text(**cfg))
        return d

    def test_the_cfg_is_read_without_its_quotes(self):
        from rommarmada import libretro
        d = self.data_dir(savefile_directory="/x/saves", sort_savefiles_enable="true")
        (d / "retroarch.cfg").write_text((d / "retroarch.cfg").read_text() + "# a comment\nbare = 5\n")
        cfg = libretro.read_cfg(d / "retroarch.cfg")
        self.assertEqual((cfg["savefile_directory"], cfg["sort_savefiles_enable"], cfg["bare"]), ("/x/saves", "true", "5"))
        self.assertEqual(libretro.read_cfg(self.tmp / "missing.cfg"), {})

    def test_where_the_save_folder_is(self):
        from rommarmada import libretro
        d = self.data_dir()
        cases = {
            "/mnt/sd/saves": Path("/mnt/sd/saves"),
            ":/saves": d / "saves",
            ":\\saves": d / "saves",
            "~/saves": self.home / "saves",
            "saves": d / "saves",
            "default": None,
            "": None,
        }
        for raw, want in cases.items():
            with self.subTest(raw):
                (d / "retroarch.cfg").write_text(cfg_text(savefile_directory=raw))
                self.assertEqual(libretro.config_of(d).save_dir, want)
        (d / "retroarch.cfg").write_text(cfg_text(savefile_directory="/x", savefiles_in_content_dir="true"))
        self.assertIsNone(libretro.config_of(d).save_dir, "saves next to the game when RetroArch says so")
        self.assertEqual(libretro.config_of(d, "/mine").save_dir, Path("/mine"), "the plugin's own setting wins")

    def test_the_data_folder_is_found_or_set(self):
        from rommarmada import libretro
        flatpak = self.home / ".var" / "app" / "org.libretro.RetroArch" / "config" / "retroarch"
        flatpak.mkdir(parents=True)
        r = libretro.resolve()
        self.assertEqual((r.data_dir, r.config.why, r.data_dir_exists), (flatpak, "found", True))
        plain = self.data_dir()
        self.assertEqual(libretro.resolve().data_dir, plain, "the usual place comes first")
        mine = self.tmp / "mine"
        r = libretro.resolve(str(mine))
        self.assertEqual((r.data_dir, r.config.why, r.data_dir_exists), (mine, "setting", False))
        shutil_rmtree(plain, flatpak)
        self.assertEqual((libretro.resolve().config.why, libretro.resolve().data_dir_exists), ("default", False))

    def test_the_game_folder_for_every_way_retroarch_sorts_saves(self):
        from rommarmada import libretro
        rom = str(self.home / "ROMs" / "gba" / f"{STEM}.gba")
        saves = self.tmp / "saves"
        d = self.data_dir(savefile_directory=str(saves))
        cfg = lambda: libretro.config_of(d)  # noqa: E731
        self.assertEqual(libretro.save_root(cfg(), rom), (saves, ""), "one folder, nothing sorted")
        (d / "retroarch.cfg").write_text(cfg_text())
        self.assertEqual(libretro.save_root(cfg(), rom)[0], Path(rom).parent, "next to the game")

        (d / "retroarch.cfg").write_text(cfg_text(savefile_directory=str(saves), sort_savefiles_enable="true"))
        root, why = libretro.save_root(cfg(), rom)
        self.assertIsNone(root, "by core: the core's folder is not known before the game has been played")
        self.assertIn("per core", why)
        (saves / "mGBA").mkdir(parents=True)
        (saves / "mGBA" / f"{STEM}.srm").write_bytes(b"s")
        (saves / "gpSP").mkdir()
        (saves / "gpSP" / "Other.srm").write_bytes(b"s")
        self.assertEqual(libretro.save_root(cfg(), rom)[0], saves / "mGBA", "found where the game's save already is")

        (d / "retroarch.cfg").write_text(cfg_text(savefile_directory=str(saves), sort_savefiles_by_content_enable="true"))
        (saves / "mGBA" / f"{STEM}.srm").unlink()
        self.assertEqual(libretro.save_root(cfg(), rom)[0], saves / "gba", "by game folder: named after the folder the game is in")

        (d / "retroarch.cfg").write_text(cfg_text(savefile_directory=str(saves), sort_savefiles_enable="true",
                                                  sort_savefiles_by_content_enable="true"))
        (saves / "mGBA" / "gba").mkdir()
        (saves / "mGBA" / "gba" / f"{STEM}.srm").write_bytes(b"s")
        self.assertEqual(libretro.save_root(cfg(), rom)[0], saves / "mGBA" / "gba")


def shutil_rmtree(*paths):
    import shutil
    for p in paths:
        shutil.rmtree(p, ignore_errors=True)


class UnitTests(Env):
    def setUp(self):
        super().setUp()
        self.root = self.tmp / "saves"
        self.root.mkdir()
        self.rom = str(self.tmp / f"{STEM}.gba")

    def locate(self):
        """What sigil itself says this game's save is: the artifact's name and its RomM hash."""
        p = subprocess.run([str(BIN / f"savebridge-{os.environ.get('ROMM_ARMADA_HELPER_ARCH', 'x86_64')}"), "locate", "--layout",
                            "libretro", "--root", str(self.root), "--content", self.rom, "--hash"], capture_output=True, text=True)
        return json.loads(p.stdout.splitlines()[-1])

    def test_a_save_alone_goes_up_as_it_is(self):
        from rommarmada import libretro
        data = os.urandom(8192)
        (self.root / f"{STEM}.srm").write_bytes(data)
        got = libretro.collect(self.root, self.rom, self.tmp / "w", None)
        self.assertEqual((got["artifact"], got["has_unit"], got["format"]), (f"{STEM}.srm", True, "file"))
        self.assertEqual(Path(got["unit_path"]).read_bytes(), data)
        self.assertEqual(got["content_hash"], hashlib.md5(data).hexdigest())
        sigil = self.locate()
        self.assertEqual((sigil["artifact"], sigil["content_hash"]), (got["artifact"], got["content_hash"]), "what sigil's row says")

    def test_with_a_clock_both_files_go_up_as_a_flat_zip(self):
        from rommarmada import libretro
        (self.root / f"{STEM}.srm").write_bytes(os.urandom(2048))
        (self.root / f"{STEM}.rtc").write_bytes(b"\x01" * 48)
        got = libretro.collect(self.root, self.rom, self.tmp / "w", None)
        self.assertEqual(got["artifact"], f"{STEM}.srm.zip")
        with zipfile.ZipFile(got["unit_path"]) as z:
            self.assertEqual(z.namelist(), [f"{STEM}.srm", f"{STEM}.rtc"])
        self.assertEqual(got["content_hash"], romm_hash(Path(got["unit_path"]).read_bytes()), "the hash RomM will give the upload")
        sigil = self.locate()
        self.assertEqual((sigil["artifact"], sigil["content_hash"]), (got["artifact"], got["content_hash"]))
        # the clock ticking is not a new save
        before = got["identity_hash"]
        (self.root / f"{STEM}.rtc").write_bytes(b"\x02" * 48)
        again = libretro.collect(self.root, self.rom, self.tmp / "w2", None)
        self.assertEqual(again["identity_hash"], before)
        self.assertNotEqual(again["content_hash"], got["content_hash"])

    def test_nothing_without_a_srm_and_changed_follows_the_last_sync(self):
        from rommarmada import libretro
        self.assertFalse(libretro.collect(self.root, self.rom, self.tmp / "w", None)["has_unit"])
        (self.root / f"{STEM}.rtc").write_bytes(b"only a clock")
        self.assertFalse(libretro.collect(self.root, self.rom, self.tmp / "w", None)["has_unit"])
        (self.root / f"{STEM}.srm").write_bytes(b"save")
        first = libretro.collect(self.root, self.rom, self.tmp / "w", None)
        same = libretro.collect(self.root, self.rom, self.tmp / "w", {"local_hash": first["content_hash"]})
        self.assertEqual((first["changed"], same["changed"]), (True, False))

    def test_restoring_a_raw_save_a_zip_and_a_clock(self):
        from rommarmada import libretro
        out = libretro.restore(b"raw save", "Other Name.srm", self.root, self.rom)
        self.assertEqual(out["written"], [f"{STEM}.srm"], "named after this device's game, not the one that made it")
        self.assertEqual((self.root / f"{STEM}.srm").read_bytes(), b"raw save")
        self.assertEqual(out["content_hash"], hashlib.md5(b"raw save").hexdigest())
        z = flat_zip({"Argosy name.srm": b"zipped save", "Argosy name.rtc": b"clock", "Argosy name.state": b"state"})
        out = libretro.restore(z, "x.srm.zip", self.root, self.rom)
        self.assertEqual(sorted(out["written"]), [f"{STEM}.rtc", f"{STEM}.srm"])
        self.assertEqual((self.root / f"{STEM}.srm").read_bytes(), b"zipped save")
        self.assertEqual((self.root / f"{STEM}.rtc").read_bytes(), b"clock")
        self.assertFalse(list(self.root.glob("*.state")), "a save state is not a save")
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), [f"{STEM}.rtc", f"{STEM}.srm"], "no temporary files left")

    def test_what_is_not_a_retroarch_save_is_refused_and_nothing_changes(self):
        from rommarmada import libretro
        from rommarmada.savefolders import ArchiveError
        (self.root / f"{STEM}.srm").write_bytes(b"mine")
        for what, data in {"a PS2 folder zip": flat_zip({"BASLUS-20152/icon.sys": b"x", "BASLUS-20152/save.dat": b"y"}),
                           "an empty file": b"", "a zip with a clock only": flat_zip({"a.rtc": b"c"}),
                           "an unsafe path": flat_zip({"../evil.srm": b"x"})}.items():
            with self.subTest(what), self.assertRaises(ArchiveError):
                libretro.restore(data, "x", self.root, self.rom)
        self.assertEqual((self.root / f"{STEM}.srm").read_bytes(), b"mine")
        self.assertFalse((self.root.parent / "evil.srm").exists())

    def test_a_failed_write_puts_the_old_files_back(self):
        from unittest import mock
        from rommarmada import libretro
        (self.root / f"{STEM}.srm").write_bytes(b"old save")
        (self.root / f"{STEM}.rtc").write_bytes(b"old clock")
        real = os.replace
        calls = []

        def flaky(src, dst):
            calls.append(dst)
            if len(calls) == 2:
                raise OSError("disk full")
            return real(src, dst)
        with mock.patch("os.replace", flaky), self.assertRaises(OSError):
            libretro.restore(flat_zip({"a.srm": b"new save", "a.rtc": b"new clock"}), "x", self.root, self.rom)
        self.assertEqual((self.root / f"{STEM}.srm").read_bytes(), b"old save")
        self.assertEqual((self.root / f"{STEM}.rtc").read_bytes(), b"old clock")


class SyncBase(Env):
    SLUG, EXT = "gb", "gb"

    def setUp(self):
        super().setUp()
        self.pid = self.mock.add_platform(self.SLUG, "Game Boy")
        self.rom = self.mock.add_rom(self.pid, STEM, [(f"{STEM}.{self.EXT}", b"GBROM" * 200)])
        self.a = Device(self, "handheld-a")
        self.b = Device(self, "handheld-b")
        for d in (self.a, self.b):
            with d.active():
                d.install(self.rom)
                d.engine()
            self.configure(d)

    def configure(self, dev, **cfg):
        folder = dev.home / ".config" / "retroarch"
        folder.mkdir(parents=True, exist_ok=True)
        saves = folder / "saves"
        (folder / "retroarch.cfg").write_text(cfg_text(**{"savefile_directory": str(saves), **cfg}))
        return saves

    def saves_dir(self, dev) -> Path:
        return dev.home / ".config" / "retroarch" / "saves"

    def play(self, dev, data: bytes, rtc: bytes | None = None, folder: Path | None = None) -> None:
        d = folder or self.saves_dir(dev)
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{STEM}.srm").write_bytes(data)
        if rtc is not None:
            (d / f"{STEM}.rtc").write_bytes(rtc)
        time.sleep(0.02)

    def sync(self, dev, **kw):
        with dev.active():
            out = dev.sync.sync([self.rom], **kw)
        self.assertEqual(len(out), 1)
        return out[0]

    def server_saves(self, slot="armadaos"):
        return sorted(self.mock.saves_of(self.rom, slot), key=lambda s: s["updated_at"], reverse=True)

    def record(self, dev):
        from rommarmada import saves
        with dev.active():
            return saves.Record(self.rom)

    def fake_proc(self, name: str, args=()) -> Path:
        proc = self.tmp / "proc"
        d = proc / "4242"
        d.mkdir(parents=True)
        (d / "comm").write_text(name + "\n")
        (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in (name, *args)) + b"\0")
        return proc


class SyncTests(SyncBase):
    def test_the_save_goes_to_romm_as_the_srm_tagged_armadaos(self):
        data = os.urandom(8192)
        self.play(self.a, data)
        r = self.sync(self.a)
        self.assertEqual(r["status"], "uploaded", r)
        (save,) = self.server_saves()
        self.assertEqual((save["slot"], save["emulator"], save["data"]), ("armadaos", "armadaos", data))
        self.assertRegex(save["file_name"], r"^Test Game \(USA\) \[.*\]\.srm$")
        self.assertEqual(save["content_hash"], hashlib.md5(data).hexdigest())
        rec = self.record(self.a).last
        self.assertEqual((rec["action"], rec["format"], rec["unit"]["kind"]), ("upload", "file", "file"))
        self.assertEqual(self.sync(self.a)["status"], "up_to_date")
        self.assertEqual(len(self.server_saves()), 1)

    def test_a_clock_goes_along_as_a_zip(self):
        self.play(self.a, os.urandom(1024), rtc=b"\x05" * 48)
        self.assertEqual(self.sync(self.a)["status"], "uploaded")
        (save,) = self.server_saves()
        self.assertRegex(save["file_name"], r"^Test Game \(USA\)\.srm \[.*\]\.zip$")      # RomM tags the artifact "<game>.srm.zip"
        self.assertEqual(zipfile.ZipFile(io.BytesIO(save["data"])).namelist(), [f"{STEM}.srm", f"{STEM}.rtc"])
        self.assertEqual(save["content_hash"], romm_hash(save["data"]))
        self.assertEqual(self.sync(self.b)["status"], "downloaded")
        self.assertEqual((self.saves_dir(self.b) / f"{STEM}.rtc").read_bytes(), b"\x05" * 48)

    def test_a_second_handheld_gets_it_and_then_follows_newer_saves(self):
        data = os.urandom(4096)
        self.play(self.a, data)
        self.sync(self.a)
        r = self.sync(self.b)
        self.assertEqual(r["status"], "downloaded", r)
        self.assertEqual((self.saves_dir(self.b) / f"{STEM}.srm").read_bytes(), data)
        self.assertEqual(self.sync(self.b)["status"], "up_to_date")
        self.assertEqual(len(self.server_saves()), 1, "nothing new was uploaded")
        time.sleep(0.02)
        self.play(self.a, b"later" * 500)
        self.assertEqual(self.sync(self.a)["status"], "uploaded")
        self.assertEqual(self.sync(self.b)["status"], "downloaded")
        self.assertEqual((self.saves_dir(self.b) / f"{STEM}.srm").read_bytes(), b"later" * 500)
        rec = self.record(self.b).last
        self.assertEqual((rec["action"], rec["format"], rec["local_hash"]), ("download", "file", self.server_saves()[0]["content_hash"]))

    def test_both_changed_asks_and_changes_nothing(self):
        self.play(self.a, b"v1" * 100)
        self.sync(self.a)
        self.sync(self.b)
        time.sleep(0.02)
        self.play(self.a, b"a's v2" * 100)
        self.sync(self.a)
        self.play(self.b, b"b's v2" * 100)
        r = self.sync(self.b)
        self.assertEqual(r["status"], "conflict", r)
        self.assertEqual((self.saves_dir(self.b) / f"{STEM}.srm").read_bytes(), b"b's v2" * 100)
        r = self.sync(self.b, resolutions={self.rom: "server"})
        self.assertEqual(r["status"], "downloaded", r)
        self.assertEqual((self.saves_dir(self.b) / f"{STEM}.srm").read_bytes(), b"a's v2" * 100)
        with self.b.active():
            from rommarmada import saves
            backups = saves.list_backups(self.rom)
        self.assertEqual(len(backups), 1)
        self.assertEqual((Path(backups[0]["path"]) / f"{STEM}.srm").read_bytes(), b"b's v2" * 100, "backed up before it was replaced")

    def test_first_contact_with_a_different_save_asks(self):
        self.play(self.a, b"on a" * 100)
        self.sync(self.a)
        self.play(self.b, b"already on b" * 100)
        r = self.sync(self.b)
        self.assertEqual((r["status"], r["conflict"]["reason"]), ("conflict", "first_contact"))
        self.assertEqual((self.saves_dir(self.b) / f"{STEM}.srm").read_bytes(), b"already on b" * 100)

    def test_a_save_made_by_another_client_in_another_slot_is_pulled_as_the_latest(self):
        self.mock.seed_save(self.rom, b"made on a phone" * 50, slot="argosy", emulator="retroarch", file_name=f"{STEM}.srm",
                            age=timedelta(minutes=5))
        r = self.sync(self.a)
        self.assertEqual(r["status"], "downloaded", r)
        self.assertEqual((self.saves_dir(self.a) / f"{STEM}.srm").read_bytes(), b"made on a phone" * 50)
        self.assertEqual(self.record(self.a).last["slot"], "argosy")
        self.play(self.a, b"then played here" * 50)
        self.assertEqual(self.sync(self.a)["status"], "uploaded")
        self.assertEqual(len(self.server_saves()), 1)
        self.assertEqual(len(self.mock.saves_of(self.rom, "argosy")), 1, "the other slot was left as it was")

    def test_something_that_is_not_a_battery_save_is_skipped_once(self):
        self.mock.seed_save(self.rom, flat_zip({"BASLUS-20152/save.dat": b"ps2"}), slot="freegosy", emulator="freegosy",
                            file_name="x.zip")
        r = self.sync(self.a)
        self.assertEqual(r["status"], "error", r)
        self.assertIn("not a RetroArch save", r["message"])
        self.assertFalse((self.saves_dir(self.a) / f"{STEM}.srm").exists())
        self.assertEqual(self.sync(self.a)["status"], "no_saves", "asked about once")

    def test_no_save_anywhere_is_not_an_error(self):
        self.assertEqual(self.sync(self.a)["status"], "no_saves")

    def test_a_save_folder_sorted_by_core_waits_until_the_game_has_been_played(self):
        saves = self.configure(self.a, sort_savefiles_enable="true")
        self.mock.seed_save(self.rom, b"from elsewhere" * 20, slot="argosy", file_name=f"{STEM}.srm")
        r = self.sync(self.a)
        self.assertEqual((r["status"], r["reason"]), ("blocked", "core_folder"), r)
        self.play(self.a, b"played once" * 20, folder=saves / "mGBA")
        r = self.sync(self.a)
        self.assertIn(r["status"], ("conflict", "uploaded"), r)

    def test_only_retroarch_running_holds_a_sync_back(self):
        self.play(self.a, b"data" * 100)
        with self.a.active():
            self.a.engine(proc_root=str(self.fake_proc("retroarch", ("-L", "/cores/gambatte_libretro.so", "game.gb"))))
        self.assertEqual(self.sync(self.a)["status"], "busy")
        self.tmp.joinpath("proc").rename(self.tmp / "proc-gone")
        with self.a.active():
            self.a.engine(proc_root=str(self.fake_proc("armsx2-qt")))          # a PS2 emulator does not matter to a Game Boy game
        self.assertEqual(self.sync(self.a)["status"], "uploaded")

    def test_the_game_screen_says_where_the_save_is(self):
        with self.a.active():
            st = self.a.sync.status(self.rom)
        self.assertEqual((st["supported"], st["emulator"]), (True, "RetroArch"))
        self.assertEqual(st["data_dir"], str(self.saves_dir(self.a)))


class MoreSystemsTests(Env):
    def test_every_listed_system_is_a_retroarch_group_and_ps2_is_not(self):
        from rommarmada import emulators
        for slug in ("gb", "gbc", "gba", "nes", "snes", "genesis-slash-megadrive", "sms", "gamegear"):
            self.assertEqual(emulators.group_for_platform(slug), "retroarch", slug)
        self.assertEqual(emulators.group_for_platform("ps2"), "ps2")
        self.assertIsNone(emulators.group_for_platform("n64"))
        self.assertNotIn("retroarch", emulators.all_ps2_process_names())
        self.assertIn("retroarch", emulators.all_process_names())

    def test_a_gba_game_syncs_without_sigil_reading_it(self):
        pid = self.mock.add_platform("gba", "Game Boy Advance")
        rid = self.mock.add_rom(pid, "Advance Game", [("Advance Game.gba", b"not a header" * 100)])
        a = Device(self, "handheld-a")
        with a.active():
            a.install(rid)
            a.engine()
        saves = a.home / ".config" / "retroarch" / "saves"
        saves.mkdir(parents=True)
        (a.home / ".config" / "retroarch" / "retroarch.cfg").write_text(cfg_text(savefile_directory=str(saves)))
        (saves / "Advance Game.srm").write_bytes(b"gba save" * 100)
        with a.active():
            (r,) = a.sync.sync([rid])
        self.assertEqual(r["status"], "uploaded", r)
        (s,) = self.mock.saves_of(rid, "armadaos")
        self.assertEqual(s["data"], b"gba save" * 100)

    def test_retroarch_is_found_by_its_process_and_its_game_by_the_command_line(self):
        from rommarmada import emulators
        proc = self.tmp / "proc"
        d = proc / "77"
        d.mkdir(parents=True)
        rom = self.tmp / "Game.gba"
        rom.write_bytes(b"x")
        (d / "comm").write_text("retroarch\n")
        (d / "cmdline").write_bytes(b"\0".join([b"retroarch", b"-L", b"/cores/mgba_libretro.so", str(rom).encode()]) + b"\0")
        (inst,) = emulators.find_instances(emulators.all_process_names(), str(proc))
        self.assertEqual(inst.rom_path(), str(rom))
        self.assertEqual(emulators.find_instances(emulators.all_ps2_process_names(), str(proc)), [])
