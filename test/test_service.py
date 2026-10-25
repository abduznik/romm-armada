# SPDX-License-Identifier: MIT
import asyncio
import importlib
import logging
import os
import shutil
import sys
import time
import types
from pathlib import Path

import fixtures
from support import BIN, Device, Env, ROOT


class SvcBase(Env):
    def setUp(self):
        super().setUp()
        self.dev = self.sign_in()
        self.proc = self.tmp / "proc"
        self.proc.mkdir()
        from rommarmada.service import Service
        from rommarmada.sigil import Savebridge
        self.svc = Service(emit=self.emit, bridge=Savebridge(BIN), settings=self.settings, proc_root=str(self.proc))
        self.svc.downloads.start()
        self.pid = self.ps2_platform()

    def tearDown(self):
        self.svc.stop()
        super().tearDown()

    def wait_for(self, cond, timeout=20, what="condition"):
        end = time.time() + timeout
        while time.time() < end:
            v = cond()
            if v:
                return v
            time.sleep(0.05)
        self.fail(f"timed out waiting for {what}")

    def start_emulator(self, rom_path=None, pid=4242, holds=()):
        """A running emulator in the fake /proc. `holds`: files it has open (what ARMSX2 does with the disc of the game it
        runs, even when it was started from its own menu and has no disc on its command line)."""
        d = self.proc / str(pid)
        d.mkdir(parents=True, exist_ok=True)
        cmd = ["/tmp/.mount_ARMSX2abc/usr/bin/armsx2-qt", "-batch", "-fullscreen"] + (["--", rom_path] if rom_path else [])
        (d / "comm").write_text("armsx2-qt\n")
        (d / "cmdline").write_bytes(b"\0".join(c.encode() for c in cmd) + b"\0")
        try:
            os.symlink("/tmp/.mount_ARMSX2abc/usr/bin/armsx2-qt", d / "exe")
        except FileExistsError:
            pass
        self.hold(*holds, pid=pid)

    def hold(self, *paths, pid=4242):
        fd = self.proc / str(pid) / "fd"
        shutil.rmtree(fd, ignore_errors=True)
        fd.mkdir()
        os.symlink("/dev/null", fd / "0")
        os.symlink("socket:[12345]", fd / "1")
        os.symlink(str(self.home / ".config" / "ARMSX2" / "logs" / "emulog.txt"), fd / "2")
        for i, target in enumerate(paths):
            os.symlink(str(target), fd / str(3 + i))

    def stop_emulator(self, pid=4242):
        shutil.rmtree(self.proc / str(pid), ignore_errors=True)


class LibraryTests(SvcBase):
    def test_overview(self):
        o = self.svc.overview()
        self.assertTrue(o["signed_in"])
        self.assertEqual(o["emulator"]["name"], "ARMSX2")
        self.assertEqual(o["emulator"]["card"]["wanted"], "folder")
        self.assertTrue(o["helper"]["ok"])
        self.assertEqual(o["library"]["root"], str(self.home / "ROMs"))
        self.assertGreater(o["library"]["free"], 0)

    def test_platforms_roms_and_the_installed_flag(self):
        self.mock.add_rom(self.pid, "Alpha", [("Alpha.iso", b"a" * 100)])
        beta = self.mock.add_rom(self.pid, "Beta", [("Beta.iso", b"b" * 100)])
        p = self.svc.platforms()["platforms"]
        self.assertEqual((p[0]["name"], p[0]["folder"], p[0]["saves"]), ("PlayStation 2", "ps2", True))
        page = self.svc.roms(self.pid, "", 0, 10)
        self.assertEqual([r["name"] for r in page["items"]], ["Alpha", "Beta"])
        self.assertEqual([r["name"] for r in self.svc.roms(self.pid, "bet")["items"]], ["Beta"])
        self.svc.install(beta)
        self.wait_for(lambda: self.svc.manifest.get(beta), what="the install")
        self.assertTrue({r["name"]: r for r in self.svc.roms(self.pid)["items"]}["Beta"]["installed"])

    def test_paging(self):
        for i in range(7):
            self.mock.add_rom(self.pid, f"Game {i}", [(f"Game {i}.iso", b"x")])
        a, b = self.svc.roms(self.pid, "", 0, 4), self.svc.roms(self.pid, "", 4, 4)
        self.assertEqual((len(a["items"]), len(b["items"]), a["total"]), (4, 3, 7))

    def test_offline_shows_the_cached_platforms_and_installed_games(self):
        rid = self.mock.add_rom(self.pid, "Kept", [("Kept.iso", b"k" * 100)])
        self.svc.platforms()
        self.svc.install(rid)
        self.wait_for(lambda: self.svc.manifest.get(rid), what="the install")
        self.mock.stop()
        self.svc._lists.clear()
        p = self.svc.platforms()
        self.assertTrue(p["stale"])
        self.assertEqual(p["platforms"][0]["name"], "PlayStation 2")
        page = self.svc.roms(self.pid)
        self.assertTrue(page["stale"])
        self.assertEqual([r["name"] for r in page["items"]], ["Kept"])

    def test_game_detail(self):
        rid = self.mock.add_rom(self.pid, "Detail", [("Detail.iso", b"d" * 500)], title_id="SLUS-20152")
        d = self.svc.rom(rid)
        self.assertEqual((d["name"], d["platform"], d["installed"], d["saves_supported"]), ("Detail", "PlayStation 2", False, True))
        self.assertEqual(d["files"][0]["name"], "Detail.iso")

    def test_the_cover_reaches_the_panel_as_a_data_uri(self):
        rid = self.mock.add_rom(self.pid, "Covered", [("Covered.iso", b"c" * 100)])
        path = "/assets/romm/resources/roms/1/1/cover/small.webp"
        self.mock.roms[rid]["path_cover_small"] = path + "?ts=2026"
        self.mock.assets[path] = b"RIFFxxxxWEBPfake"
        uri = self.svc.rom(rid)["cover"]
        self.assertTrue(uri.startswith("data:image/webp;base64,"), uri[:40])
        self.mock.stop()                                           # the second look comes from the cache
        self.assertEqual(self.svc._cover_data(self.svc.client(), self.mock.roms[rid]), uri)

    def test_a_game_without_a_cover_has_none_and_no_error(self):
        rid = self.mock.add_rom(self.pid, "Plain", [("Plain.iso", b"p" * 100)])
        self.assertEqual(self.svc.rom(rid)["cover"], "")

    def test_uninstall(self):
        rid = self.mock.add_rom(self.pid, "Bye", [("Bye.iso", b"b" * 100)])
        self.svc.install(rid)
        self.wait_for(lambda: self.svc.manifest.get(rid))
        self.assertTrue(self.svc.uninstall(rid)["ok"])
        self.assertFalse((self.home / "ROMs" / "ps2" / "Bye.iso").exists())


class InstallPullsTheSaveTests(SvcBase):
    def test_a_game_installed_on_a_new_device_gets_its_save_from_romm(self):
        iso = fixtures.make_ps2_iso("SLUS-20152")
        rid = self.mock.add_rom(self.pid, "Test Game (USA)", [("Test Game (USA).iso", iso)], title_id="SLUS-20152")
        # another handheld has played it and uploaded
        other = Device(self, "other")
        with other.active():
            other.install(rid)
            other.engine()
            other.play(b"progress from the other handheld" * 20)
            self.assertEqual(other.sync.sync([rid])[0]["status"], "uploaded")
        # this one installs it
        self.svc.install(rid)
        card = self.home / ".config" / "ARMSX2" / "memcards" / "Mcd001.ps2" / "BASLUS-20152" / "save.dat"
        self.wait_for(card.exists, what="the save to arrive after the install")
        self.assertEqual(card.read_bytes(), b"progress from the other handheld" * 20)
        self.assertEqual(self.svc.manifest.get(rid)["identity"]["title_id"], "SLUS-20152")
        self.assertTrue((card.parent.parent / "_pcsx2_superblock").is_file())


class WatcherTests(SvcBase):
    def setUp(self):
        super().setUp()
        self.rid = self.mock.add_rom(self.pid, "Test Game (USA)", [("Test Game (USA).iso", fixtures.make_ps2_iso("SLUS-20152"))],
                                     title_id="SLUS-20152")
        self.entry = self.install_ps2_game_again()
        self.card = self.home / ".config" / "ARMSX2" / "memcards" / "Mcd001.ps2"

    def install_ps2_game_again(self, rid=None):
        from rommarmada import library
        rom = self.mock.roms[rid or self.rid]
        lay = library.Layout(self.settings.get("library.root"), "esde", {})
        f = rom["files"][0]
        dest = lay.destination(rom, f)
        fixtures.write(str(dest), self.mock.file_data[f["id"]])
        entry = library.make_entry(rom, lay, [(f, dest)], dest)
        self.svc.manifest.put(entry)
        return entry

    def play(self, content):
        from rommarmada.sigil import Savebridge
        if not (self.card / "_pcsx2_superblock").exists():
            Savebridge(BIN).init_folder_card(str(self.home / ".config" / "ARMSX2"), "Mcd001.ps2")
        folder = self.card / "BASLUS-20152"
        folder.mkdir(exist_ok=True)
        (folder / "icon.sys").write_bytes(b"PS2D" + b"\0" * 60)
        (folder / "save.dat").write_bytes(content)

    def test_closing_the_emulator_pushes_the_save_and_reports_play_time(self):
        w = self.svc.watcher
        self.start_emulator(self.entry["main_file"])
        w.tick()
        self.assertEqual(w.session["rom_id"], self.rid)
        self.assertIn("emulator_started", [e for e, _ in self.events])
        self.play(b"a good session" * 50)
        w.session["started"] -= 120                                   # two minutes of play
        self.stop_emulator()
        import rommarmada.watcher as wm
        wm.EXIT_SETTLE = 0
        w.tick()
        self.assertIsNone(w.session)
        self.wait_for(lambda: self.mock.saves_of(self.rid, "armadaos"), what="the push after exit")
        self.assertEqual(len(self.mock.saves_of(self.rid, "armadaos")), 1)
        self.assertIn("emulator_stopped", [e for e, _ in self.events])
        # play time went into the queue and goes to RomM on a later tick
        w.tick()
        self.wait_for(lambda: self.mock.play_sessions, what="the play session")
        ps = self.mock.play_sessions[0]
        self.assertEqual((ps["rom_id"], ps["save_slot"]), (self.rid, "armadaos"))
        self.assertGreaterEqual(ps["duration_ms"], 120_000)

    def test_a_short_session_is_not_reported(self):
        w = self.svc.watcher
        self.start_emulator(self.entry["main_file"])
        w.tick()
        self.stop_emulator()
        import rommarmada.watcher as wm
        wm.EXIT_SETTLE = 0
        w.tick()
        w.tick()
        time.sleep(0.3)
        self.assertEqual(self.mock.play_sessions, [])

    def test_an_emulator_started_from_its_own_menu_still_syncs_everything_changed(self):
        w = self.svc.watcher
        self.start_emulator(None)
        w.tick()
        self.assertIsNone(w.session["rom_id"])
        self.play(b"unknown game session" * 20)
        self.stop_emulator()
        import rommarmada.watcher as wm
        wm.EXIT_SETTLE = 0
        w.tick()
        self.wait_for(lambda: self.mock.saves_of(self.rid), what="the push after exit")

    def test_a_game_started_from_the_emulators_menu_is_found_by_the_disc_it_holds_open(self):
        w = self.svc.watcher
        self.start_emulator(None, holds=[self.entry["main_file"]])
        w.tick()
        self.assertIsNone(w.session["rom_id"], "one look is not enough: the game list scan opens discs too")
        w.tick()
        self.assertEqual((w.session["rom_id"], w.session["found_by"]), (self.rid, "open disc image"))
        self.assertEqual([n for e, n in self.events if e == "emulator_started"][-1]["name"], "Test Game (USA)")
        self.play(b"played from the menu" * 40)
        w.session["started"] -= 180
        self.stop_emulator()
        import rommarmada.watcher as wm
        wm.EXIT_SETTLE = 0
        w.tick()
        self.wait_for(lambda: self.mock.saves_of(self.rid, "armadaos"), what="the push after exit")
        w.tick()
        self.wait_for(lambda: self.mock.play_sessions, what="the play time")
        self.assertEqual(self.mock.play_sessions[0]["rom_id"], self.rid)
        self.assertGreaterEqual(self.mock.play_sessions[0]["duration_ms"], 180_000)

    def test_a_disc_opened_for_a_moment_is_not_taken_for_the_game(self):
        w = self.svc.watcher
        self.start_emulator(None, holds=[self.entry["main_file"]])
        w.tick()
        self.hold()                                                   # the scan closed it again
        w.tick()
        self.hold(self.entry["main_file"])
        w.tick()
        self.assertIsNone(w.session["rom_id"])
        w.tick()                                                      # held on two looks in a row now
        self.assertEqual(w.session["rom_id"], self.rid)

    def test_two_discs_held_at_once_are_not_guessed_between(self):
        rid2 = self.mock.add_rom(self.pid, "Other Game", [("Other Game.iso", fixtures.make_ps2_iso("SLES-50000"))], title_id="SLES-50000")
        other = self.install_ps2_game_again(rid2)
        w = self.svc.watcher
        self.start_emulator(None, holds=[self.entry["main_file"], other["main_file"]])
        for _ in range(4):
            w.tick()
        self.assertIsNone(w.session["rom_id"])
        self.hold(other["main_file"])
        w.tick()
        w.tick()
        self.assertEqual(w.session["rom_id"], rid2)

    def test_a_file_that_is_not_an_installed_game_is_not_a_game(self):
        stranger = self.home / "Elsewhere" / "Not Ours.iso"
        fixtures.write(str(stranger), b"x" * 100)
        w = self.svc.watcher
        self.start_emulator(None, holds=[stranger, self.home / ".config" / "ARMSX2" / "bios" / "x.bin"])
        for _ in range(3):
            w.tick()
        self.assertIsNone(w.session["rom_id"])

    def test_after_a_game_found_by_its_disc_the_other_games_are_checked_too_but_this_one_first(self):
        rid2 = self.mock.add_rom(self.pid, "Other Game", [("Other Game.iso", fixtures.make_ps2_iso("SLES-50000"))], title_id="SLES-50000")
        self.install_ps2_game_again(rid2)
        w = self.svc.watcher
        pushed = []
        w._spawn = lambda fn, ids: pushed.append(list(ids))
        # by the command line: only that game
        self.start_emulator(self.entry["main_file"])
        w.tick()
        self.stop_emulator()
        w.tick()
        self.assertEqual(pushed.pop(), [self.rid])
        # by the disc it holds: all of them, with the one it held first
        other = self.svc.manifest.get(rid2)
        self.start_emulator(None, holds=[other["main_file"]])
        w.tick()
        w.tick()
        self.stop_emulator()
        w.tick()
        ids = pushed.pop()
        self.assertEqual(ids[0], rid2)
        self.assertEqual(sorted(ids), sorted([self.rid, rid2]))

    def test_a_game_the_emulator_is_playing_cannot_be_uninstalled(self):
        self.start_emulator(None, holds=[self.entry["main_file"]])         # started from its menu: the disc is open
        r = self.svc.uninstall(self.rid)
        self.assertEqual((r["ok"], r["message"]), (False, "The game is running"))
        self.assertTrue(Path(self.entry["main_file"]).exists())
        self.start_emulator(self.entry["main_file"], holds=[])             # or on its command line
        self.assertFalse(self.svc.uninstall(self.rid)["ok"])
        self.stop_emulator()
        self.assertTrue(self.svc.uninstall(self.rid)["ok"])
        self.assertFalse(Path(self.entry["main_file"]).exists())

    def test_an_emulator_that_stays_unrecognised_is_described_once_in_the_log(self):
        import rommarmada.watcher as wm
        lines = []
        real = wm.log.info
        wm.log.info = lambda msg, *a: lines.append(msg % a if a else msg)
        try:
            w = self.svc.watcher
            self.start_emulator(None, holds=[self.home / "Elsewhere" / "Mystery.iso", "/usr/lib/libx.so"])
            w.tick()
            w.session["started"] -= 60
            for _ in range(3):
                w.tick()
        finally:
            wm.log.info = real
        said = [l for l in lines if l.startswith("no installed game recognised")]
        self.assertEqual(len(said), 1, lines)
        self.assertIn("Mystery.iso", said[0])
        self.assertNotIn("libx.so", said[0], "system libraries are not interesting")

    def test_push_on_exit_can_be_turned_off(self):
        self.settings.update({"saves": {"push_on_exit": False}})
        w = self.svc.watcher
        self.start_emulator(self.entry["main_file"])
        w.tick()
        self.play(b"x" * 100)
        self.stop_emulator()
        w.tick()
        time.sleep(0.5)
        self.assertEqual(self.mock.saves, [])

    def test_the_idle_pull_brings_down_a_newer_save(self):
        other = Device(self, "other")
        with other.active():
            other.install(self.rid)
            other.engine()
            other.play(b"played elsewhere" * 30)
            other.sync.sync([self.rid])
        w = self.svc.watcher
        w._next_pull = 0
        w.tick()
        save = self.card / "BASLUS-20152" / "save.dat"
        self.wait_for(save.exists, what="the pull")
        self.assertEqual(save.read_bytes(), b"played elsewhere" * 30)

    def test_no_pull_while_the_emulator_runs(self):
        other = Device(self, "other")
        with other.active():
            other.install(self.rid)
            other.engine()
            other.play(b"played elsewhere" * 30)
            other.sync.sync([self.rid])
        w = self.svc.watcher
        self.start_emulator(self.entry["main_file"])
        w._next_pull = 0
        w.tick()
        time.sleep(0.5)
        self.assertFalse((self.card / "BASLUS-20152").exists())

    def test_games_sent_from_romms_web_ui_are_downloaded_and_reported(self):
        rid2 = self.mock.add_rom(self.pid, "Sent To Device", [("Sent To Device.iso", b"s" * 3000)])
        self.settings.update({"library": {"remote_installs": True}})
        self.mock.install_requests.append({"id": "req1", "device_id": self.dev, "rom_id": rid2,
                                           "file_ids": [self.mock.roms[rid2]["files"][0]["id"]], "status": "pending"})
        w = self.svc.watcher
        w.tick()
        self.wait_for(lambda: self.svc.manifest.get(rid2), what="the game sent from the web UI")
        w.tick()
        self.assertEqual(self.mock.install_requests[0]["status"], "done")

    def test_an_already_installed_game_is_reported_as_such(self):
        self.settings.update({"library": {"remote_installs": True}})
        self.mock.install_requests.append({"id": "req2", "device_id": self.dev, "rom_id": self.rid,
                                           "file_ids": [], "status": "pending"})
        self.svc.watcher.tick()
        self.assertEqual(self.mock.install_requests[0]["status"], "already_installed")


class CardSetupTests(SvcBase):
    """Memory card slot 1 becomes a folder card before a game is played, without waiting for a game to be installed."""

    def exe(self):
        p = self.home / "Applications" / "ARMSX2.AppImage"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"not really an emulator")

    @property
    def card(self):
        return self.home / ".config" / "ARMSX2" / "memcards" / "Mcd001.ps2"

    def test_a_fresh_install_gets_a_folder_card_before_the_emulator_ever_runs(self):
        self.exe()
        self.svc.setup_cards("test")
        self.assertTrue(self.card.is_dir())
        self.assertTrue((self.card / "_pcsx2_superblock").is_file(), "formatted, so the emulator opens it as it is")

    def test_nothing_is_made_for_an_emulator_that_is_not_there(self):
        self.svc.setup_cards("test")
        self.assertFalse((self.home / ".config" / "ARMSX2").exists())

    def test_an_empty_file_card_from_the_emulators_first_start_is_replaced_and_kept_aside(self):
        self.exe()
        self.card.parent.mkdir(parents=True)
        self.card.write_bytes(b"\xff" * 8650752)
        self.svc.setup_cards("test")
        self.assertTrue(self.card.is_dir())
        self.assertTrue(any(p.name.startswith("Mcd001.ps2.empty-") for p in self.card.parent.iterdir()))

    def test_a_card_with_saves_is_never_touched(self):
        self.exe()
        sb = self.savebridge()
        root = str(self.home / ".config" / "ARMSX2")
        os.makedirs(root, exist_ok=True)
        unit = fixtures.write(str(self.tmp / "u.zip"), fixtures.make_save_zip("BASLUS-20152"))
        sb.restore(layout="pcsx2_standalone", root=root, unit=Path(unit), rom=None, platform_slug="ps2", content="/x/T.iso",
                   identity={"platform": "ps2", "title_id": "SLUS-20152", "save_id": "BASLUS-20152"})
        before = self.card.read_bytes()
        self.assertTrue(self.card.is_file())
        self.svc.setup_cards("test")
        self.assertTrue(self.card.is_file())
        self.assertEqual(self.card.read_bytes(), before)

    def test_not_while_the_emulator_runs(self):
        self.exe()
        self.start_emulator(None)
        self.svc.setup_cards("test")
        self.assertFalse(self.card.exists())

    def test_the_settings_can_turn_it_off(self):
        self.exe()
        self.settings.update({"saves": {"card_type": "auto"}})
        self.svc.setup_cards("test")
        self.assertFalse(self.card.exists(), "'use what the emulator has' means leave the cards alone")
        self.settings.update({"saves": {"card_type": "folder", "enabled": False}})
        self.svc.setup_cards("test")
        self.assertFalse(self.card.exists())

    def test_it_happens_when_the_plugin_starts_and_when_the_device_is_paired(self):
        self.exe()
        self.svc.start()
        self.wait_for(self.card.is_dir, what="the card made at start")
        shutil.rmtree(self.card)
        self.svc.after_connect()
        self.wait_for(self.card.is_dir, what="the card made after pairing")

    def test_after_the_emulator_closes_the_empty_card_its_first_start_made_is_replaced(self):
        self.exe()
        w = self.svc.watcher
        self.start_emulator(None)
        w.tick()
        self.card.parent.mkdir(parents=True)
        self.card.write_bytes(b"\xff" * 8650752)             # what the first start leaves behind
        self.stop_emulator()
        w.tick()
        self.wait_for(self.card.is_dir, what="the card replaced after the emulator closed")


class RpcTests(SvcBase):
    """main.py under a stand-in for Decky's module."""

    def setUp(self):
        super().setUp()
        self.emitted = []
        fake = types.ModuleType("decky")
        fake.logger = logging.getLogger("decky-test")

        async def emit(event, *args):
            self.emitted.append((event, args))
        fake.emit = emit
        self._old_decky = sys.modules.get("decky")
        sys.modules["decky"] = fake
        sys.modules.pop("main", None)
        sys.path.insert(0, str(ROOT))
        self.main = importlib.import_module("main")

    def tearDown(self):
        sys.modules.pop("main", None)
        if self._old_decky is not None:
            sys.modules["decky"] = self._old_decky
        else:
            sys.modules.pop("decky", None)
        sys.path.remove(str(ROOT))
        super().tearDown()

    def test_the_panel_can_do_the_whole_connect_and_browse_flow(self):
        async def go():
            self.settings.clear_auth()                     # the plugin loads its own settings when it starts
            p = self.main.Plugin()
            await p._main()
            try:
                self.assertFalse((await p.overview())["signed_in"])
                bad = await p.connect_code(self.url, "abc")
                self.assertEqual((bad["ok"], bad["error"]), (False, "bad_code"))
                ok = await p.connect_code(self.url, self.mock.issue_pair_code())
                self.assertTrue(ok["ok"], ok)
                self.assertTrue((await p.overview())["signed_in"])
                self.mock.add_rom(self.pid, "Hello", [("Hello.iso", b"h" * 10)])
                plats = await p.platforms()
                self.assertEqual(plats["platforms"][0]["name"], "PlayStation 2")
                roms = await p.roms(self.pid)
                self.assertEqual(roms["items"][0]["name"], "Hello")
                err = await p.rom(999999)
                self.assertFalse(err["ok"])
                up = await p.settings_set({"saves": {"slot": ""}})
                self.assertFalse(up["ok"])
                self.assertIn("saves.slot", up["errors"])
                self.assertEqual((await p.disconnect())["ok"], True)
                self.assertFalse((await p.overview())["signed_in"])
            finally:
                await p._unload()
        asyncio.run(go())

    def test_the_panel_can_look_at_and_install_bios_files(self):
        data = bytearray((b"\x42" * 1024) * 4096)       # 4 MiB, the size of a PS2 BIOS
        data[10048:10064] = b"RESET" + b"\0" * 11        # and the ROM directory entry the emulator looks for
        fid = self.mock.add_firmware(self.pid, "scph70012.bin", bytes(data))

        async def go():
            p = self.main.Plugin()
            await p._main()
            try:
                near = await p.bios_status(False)
                self.assertEqual((near["ok"], near["remote"], near["has_bios"]), (True, None, False))
                st = await p.bios_status()
                self.assertEqual([i["name"] for i in st["remote"]["items"]], ["scph70012.bin"])
                self.assertEqual((await p.overview())["emulator"]["bios"]["has_bios"], False)
                done = await p.bios_install([str(fid)])            # ids arrive as the UI sends them
                self.assertTrue(done["ok"], done)
                self.assertEqual(done["report"]["installed"], ["scph70012.bin"])
                self.assertEqual((await p.overview())["emulator"]["bios"]["has_bios"], True)
                again = await p.bios_install()
                self.assertTrue(again["ok"])
                self.assertEqual(again["report"]["installed"], [])
                self.mock.tokens.clear()
                refused = await p.bios_status()
                self.assertFalse(refused["remote"]["ok"])
            finally:
                await p._unload()
        asyncio.run(go())

    def test_events_reach_decky(self):
        async def go():
            p = self.main.Plugin()
            await p._main()
            try:
                p._emit("sync_result", {"x": 1})
                await asyncio.sleep(0.1)
            finally:
                await p._unload()
        asyncio.run(go())
        self.assertIn(("sync_result", ({"x": 1},)), self.emitted)


class FirstRunTests(Env):
    """A brand-new install: default settings, nothing overridden, as the user meets it."""

    def test_pair_install_and_find_the_game_where_es_de_looks(self):
        os.environ["ROMM_ARMADA_HOME"] = str(self.tmp / "fresh-install")
        from rommarmada.service import Service
        from rommarmada.sigil import Savebridge
        svc = Service(emit=self.emit, bridge=Savebridge(BIN), proc_root=str(self.tmp / "proc"))
        (self.tmp / "proc").mkdir()
        svc.downloads.start()
        try:
            self.assertFalse(svc.overview()["signed_in"])
            svc.connector.pair_with_code(self.url, self.mock.issue_pair_code())
            svc.after_connect()
            self.assertTrue(svc.overview()["signed_in"])
            pid = self.mock.add_platform("ps2", "PlayStation 2")
            rid = self.mock.add_rom(pid, "Fresh Game (USA)", [("Fresh Game (USA).iso", fixtures.make_ps2_iso("SLUS-20152"))],
                                    title_id="SLUS-20152")
            svc.install(rid)
            end = time.time() + 20
            while time.time() < end and not svc.manifest.get(rid):
                time.sleep(0.05)
            entry = svc.manifest.get(rid)
            self.assertIsNotNone(entry)
            self.assertEqual(entry["main_file"], str(self.home / "ROMs" / "ps2" / "Fresh Game (USA).iso"))
            self.assertTrue(Path(entry["main_file"]).is_file())
            self.assertFalse((Path.cwd() / "~").exists(), "no literal ~ folder in the working directory")
            self.assertEqual(svc.overview()["library"]["root"], str(self.home / "ROMs"))
        finally:
            svc.stop()
