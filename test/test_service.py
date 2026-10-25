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

    def start_emulator(self, rom_path=None, pid=4242):
        d = self.proc / str(pid)
        d.mkdir(parents=True, exist_ok=True)
        cmd = ["/tmp/.mount_ARMSX2abc/usr/bin/armsx2-qt", "-batch", "-fullscreen"] + (["--", rom_path] if rom_path else [])
        (d / "comm").write_text("armsx2-qt\n")
        (d / "cmdline").write_bytes(b"\0".join(c.encode() for c in cmd) + b"\0")
        try:
            os.symlink("/tmp/.mount_ARMSX2abc/usr/bin/armsx2-qt", d / "exe")
        except FileExistsError:
            pass

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

    def install_ps2_game_again(self):
        from rommarmada import library
        rom = self.mock.roms[self.rid]
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
        self.wait_for(lambda: self.mock.saves_of(self.rid, "autosave"), what="the push after exit")
        self.assertEqual(len(self.mock.saves_of(self.rid, "autosave")), 1)
        self.assertIn("emulator_stopped", [e for e, _ in self.events])
        # play time went into the queue and goes to RomM on a later tick
        w.tick()
        self.wait_for(lambda: self.mock.play_sessions, what="the play session")
        ps = self.mock.play_sessions[0]
        self.assertEqual((ps["rom_id"], ps["save_slot"]), (self.rid, "autosave"))
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
