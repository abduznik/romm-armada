# SPDX-License-Identifier: MIT
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

import fixtures
import mock_romm
from support import BIN, Env, ROOT


class SettingsTests(Env):
    def test_defaults_and_persistence(self):
        s = self.settings
        self.assertEqual(s.get("saves.card_type"), "folder")          # folder cards are the default
        self.assertEqual(s.get("saves.slot"), "autosave")
        self.assertEqual(s.get("library.folder_style"), "esde")
        s.update({"saves": {"slot": "mine"}, "library": {"max_concurrent": 2}})
        from rommarmada.config import Settings
        again = Settings()
        again.load()
        self.assertEqual(again.get("saves.slot"), "mine")
        self.assertEqual(again.get("library.max_concurrent"), 2)

    def test_the_default_download_location_is_the_real_home_folder(self):
        from rommarmada.config import Settings
        fresh = Settings(self.tmp / "nothing-here.json")
        self.assertEqual(fresh.get("library.root"), str(self.home / "ROMs"))
        from rommarmada import library
        self.assertEqual(library.Layout("~/ROMs", "esde", {}).root, self.home / "ROMs")     # and a stray "~" is fixed too

    def test_bad_values_change_nothing(self):
        changed, errors = self.settings.update({"saves": {"slot": "ok"}, "library": {"max_concurrent": 9}})
        self.assertEqual(changed, {})
        self.assertIn("library.max_concurrent", errors)
        self.assertEqual(self.settings.get("saves.slot"), "autosave")   # the valid half was not applied either
        _, errors = self.settings.update({"nope": {"x": 1}})
        self.assertIn("nope", errors)

    def test_server_url_is_cleaned(self):
        from rommarmada.config import normalize_server_url, SettingsError
        self.assertEqual(normalize_server_url("192.168.1.5:8080"), "http://192.168.1.5:8080")
        self.assertEqual(normalize_server_url("https://romm.example.com/"), "https://romm.example.com")
        self.assertEqual(normalize_server_url("https://x.org/romm/api"), "https://x.org/romm")
        with self.assertRaises(SettingsError):
            normalize_server_url("ftp://x")

    def test_token_never_leaves_the_backend(self):
        self.settings.update({"auth": {"token": "rmm_secret"}})
        pub = self.settings.as_dict()
        self.assertNotIn("token", pub["auth"])
        self.assertTrue(pub["auth"]["signed_in"])
        self.assertEqual(oct(os.stat(self.settings.path).st_mode & 0o777), "0o600")

    def test_platform_folder_overrides_must_be_plain_names(self):
        _, errors = self.settings.update({"library": {"platform_dirs": {"ps2": "../etc"}}})
        self.assertTrue(errors)
        changed, errors = self.settings.update({"library": {"platform_dirs": {"ps2": "PS2 Games"}}})
        self.assertFalse(errors)


class PairingCodeTests(Env):
    def setUp(self):
        super().setUp()
        from rommarmada.connect import Connector
        self.conn = Connector(self.settings)

    def test_code_format_is_checked_before_asking_the_server(self):
        from rommarmada.connect import ConnectError
        for bad, word in (("ABC", "8 characters"), ("ABCD-EFG0", "never has 0"), ("", "8 characters")):
            with self.assertRaises(ConnectError) as cm:
                self.conn.pair_with_code(self.url, bad)
            self.assertEqual(cm.exception.code, "bad_code")
            self.assertIn(word, cm.exception.message)
        self.assertEqual(self.mock.requests, [])

    def test_typed_code_signs_in_and_registers_the_device(self):
        code = self.mock.issue_pair_code()
        done = self.conn.pair_with_code(self.url, code[:4] + "-" + code[4:].lower())   # dashes and case are forgiven
        self.assertEqual(done["username"], "tester")
        self.assertEqual(done["method"], "pairing")
        self.assertTrue(done["device_id"])
        self.assertEqual(done["missing_scopes"], [])
        s = self.settings
        self.assertTrue(s.signed_in)
        self.assertEqual(s.get("server.url"), self.url)
        self.assertEqual(s.get("auth.device_id"), done["device_id"])
        self.assertIn(done["device_id"], self.mock.devices)

    def test_code_works_once(self):
        from rommarmada.connect import ConnectError
        code = self.mock.issue_pair_code()
        self.conn.pair_with_code(self.url, code)
        with self.assertRaises(ConnectError) as cm:
            self.conn.pair_with_code(self.url, code)
        self.assertEqual(cm.exception.code, "code_expired")
        self.assertIn("60 seconds", cm.exception.message)

    def test_expired_code(self):
        from rommarmada.connect import ConnectError
        code = self.mock.issue_pair_code(ttl=-1)
        with self.assertRaises(ConnectError) as cm:
            self.conn.pair_with_code(self.url, code)
        self.assertEqual(cm.exception.code, "code_expired")
        self.assertFalse(self.settings.signed_in)

    def test_rate_limit_is_explained(self):
        from rommarmada.connect import ConnectError
        for _ in range(5):
            with self.assertRaises(ConnectError):
                self.conn.pair_with_code(self.url, "AAAAAAAA")
        with self.assertRaises(ConnectError) as cm:
            self.conn.pair_with_code(self.url, "AAAAAAAA")
        self.assertEqual(cm.exception.code, "rate_limited")

    def test_token_without_permissions_is_reported(self):
        code = self.mock.issue_pair_code(scopes=["me.read", "devices.write", "devices.read", "platforms.read", "roms.read"])
        done = self.conn.pair_with_code(self.url, code)
        self.assertIn("assets.read", done["missing_scopes"])

    def test_pairing_again_finds_the_same_device(self):
        a = self.conn.pair_with_code(self.url, self.mock.issue_pair_code())
        self.settings.clear_auth()
        b = self.conn.pair_with_code(self.url, self.mock.issue_pair_code())
        self.assertEqual(a["device_id"], b["device_id"])      # same hostname + platform: RomM hands back the device
        self.assertEqual(len(self.mock.devices), 1)

    def test_two_handhelds_with_the_same_hostname_stay_two_devices(self):
        """RomM merges devices that share hostname and platform; each install must register under its own."""
        import socket
        from rommarmada.connect import Connector
        from rommarmada.config import Settings
        socket_name = socket.gethostname
        socket.gethostname = lambda: "armada"                      # both of them
        try:
            self.conn.pair_with_code(self.url, self.mock.issue_pair_code())
            first = self.settings.get("auth.device_id")
            os.environ["ROMM_ARMADA_HOME"] = str(self.tmp / "second-install")       # another install: its own id
            other = Settings(self.tmp / "second-install" / "settings.json")
            other.load()
            Connector(other).pair_with_code(self.url, self.mock.issue_pair_code())
        finally:
            socket.gethostname = socket_name
        self.assertNotEqual(first, other.get("auth.device_id"))
        self.assertEqual(len(self.mock.devices), 2)

    def test_unreachable_server_is_explained(self):
        from rommarmada.connect import ConnectError
        with self.assertRaises(ConnectError) as cm:
            self.conn.pair_with_code("http://127.0.0.1:9", "ABCDEFGH")
        self.assertEqual(cm.exception.code, "unreachable")


class DeviceFlowTests(Env):
    def setUp(self):
        super().setUp()
        from rommarmada.connect import Connector
        self.conn = Connector(self.settings)

    def test_show_a_code_then_approve_in_the_web_ui(self):
        flow = self.conn.start_device_flow(self.url)
        self.assertEqual(len(flow["user_code"]), 9)                    # XXXX-XXXX
        self.assertIn("/pair/device?user_code=", flow["verify_url"])
        self.conn._flow["next_poll"] = 0
        self.assertEqual(self.conn.poll_device_flow()["status"], "pending")
        self.mock.approve_flow(flow["user_code"])
        self.conn._flow["next_poll"] = 0
        done = self.conn.poll_device_flow()
        self.assertEqual(done["status"], "approved")
        self.assertTrue(self.settings.signed_in)
        self.assertIn(self.settings.get("auth.device_id"), self.mock.devices)
        self.assertFalse(self.conn.flow_view()["active"])

    def test_denied(self):
        flow = self.conn.start_device_flow(self.url)
        self.mock.deny_flow(flow["user_code"])
        self.conn._flow["next_poll"] = 0
        self.assertEqual(self.conn.poll_device_flow()["status"], "denied")
        self.assertFalse(self.settings.signed_in)

    def test_polling_faster_than_the_interval_does_not_hit_the_server(self):
        self.conn.start_device_flow(self.url)
        before = len(self.mock.requests)
        for _ in range(5):
            self.conn.poll_device_flow()
        self.assertEqual(len(self.mock.requests), before)


class OtherSignInTests(Env):
    def setUp(self):
        super().setUp()
        from rommarmada.connect import Connector
        self.conn = Connector(self.settings)

    def test_password_is_exchanged_for_a_token_and_not_kept(self):
        done = self.conn.sign_in_password(self.url, "tester", "secret")
        self.assertEqual(done["method"], "password")
        self.assertNotIn("secret", self.settings.path.read_text())
        self.assertNotEqual(self.settings.get("auth.token"), "")

    def test_wrong_password(self):
        from rommarmada.connect import ConnectError
        with self.assertRaises(ConnectError) as cm:
            self.conn.sign_in_password(self.url, "tester", "nope")
        self.assertEqual(cm.exception.code, "bad_credentials")

    def test_pasted_token(self):
        tok = self.mock.make_token()
        self.assertEqual(self.conn.sign_in_token(self.url, tok)["method"], "token")

    def test_rejected_token(self):
        from rommarmada.connect import ConnectError
        with self.assertRaises(ConnectError) as cm:
            self.conn.sign_in_token(self.url, "rmm_wrong")
        self.assertEqual(cm.exception.code, "rejected")

    def test_probe_reports_version_and_pairing_support(self):
        info = self.conn.probe(self.url)
        self.assertEqual(info["version"], "5.4.1")
        self.assertTrue(info["supports_pairing"])
        self.mock.version = "4.9.0"
        self.assertFalse(self.conn.probe(self.url)["supports_pairing"])


class HelperTests(Env):
    """The native helper, on the architecture this machine runs (and aarch64 under qemu when it is there)."""

    def setUp(self):
        super().setUp()
        self.rom = fixtures.write(str(self.tmp / "rom" / "Test Game (USA).iso"), fixtures.make_ps2_iso())
        self.unit = fixtures.write(str(self.tmp / "unit.zip"), fixtures.make_save_zip())
        self.root = str(self.home / ".config" / "ARMSX2")
        os.makedirs(self.root)
        self.sb = self.savebridge()

    def kw(self, **extra):
        return dict(layout="pcsx2_standalone", root=self.root, rom=self.rom, platform_slug="ps2", **extra)

    def test_runs_and_names_its_architecture(self):
        v = self.sb.version()
        self.assertEqual(v["arch"], os.environ["ROMM_ARMADA_HELPER_ARCH"])
        self.assertIn("5d4ddb41cc22", v["sigil_pin"])

    def test_identifies_a_disc(self):
        got = self.sb.identify(self.rom, "ps2")
        self.assertEqual((got["title_id"], got["save_id"]), ("SLUS-20152", "BASLUS-20152"))

    def test_folder_card_round_trip(self):
        r = self.sb.init_folder_card(self.root, "Mcd001.ps2")
        self.assertEqual(r["status"], "created")
        card = Path(self.root) / "memcards" / "Mcd001.ps2"
        self.assertTrue(card.is_dir())
        self.assertEqual((card / "_pcsx2_superblock").stat().st_size, 8192)
        self.assertEqual(self.sb.init_folder_card(self.root, "Mcd001.ps2")["status"], "exists")

        got = self.sb.restore(unit=Path(self.unit), **self.kw(state_out=self.tmp / "s.bin"))
        self.assertEqual(got["shape"], "single")
        # the game's save folder was written, and the card is still a folder, not an 8 MB file
        self.assertTrue((card / "BASLUS-20152" / "save.dat").is_file())
        self.assertTrue(card.is_dir())

        out = self.tmp / "out.ps2"
        c = self.sb.collect(out=out, **self.kw(state_in=self.tmp / "s.bin"))
        self.assertTrue(c["has_unit"])
        self.assertFalse(c["changed"])                    # nothing changed since the restore
        import hashlib
        self.assertEqual(hashlib.md5(out.read_bytes()).hexdigest(), c["content_hash"])   # RomM's content_hash
        self.assertEqual(out.stat().st_size, 8650752)

    def test_a_file_card_and_a_folder_card_give_the_same_unit(self):
        # file card: restore into an empty root (sigil makes an 8 MB card)
        self.sb.restore(unit=Path(self.unit), **self.kw())
        file_card = Path(self.root) / "memcards" / "Mcd001.ps2"
        self.assertTrue(file_card.is_file())
        a = self.tmp / "a.ps2"
        ca = self.sb.collect(out=a, **self.kw())
        # folder card
        root2 = str(self.tmp / "second")
        self.sb.init_folder_card(root2, "Mcd001.ps2")
        self.sb.restore(unit=Path(self.unit), layout="pcsx2_standalone", root=root2, rom=self.rom, platform_slug="ps2")
        b = self.tmp / "b.ps2"
        cb = self.sb.collect(out=b, layout="pcsx2_standalone", root=root2, rom=self.rom, platform_slug="ps2")
        self.assertEqual(ca["content_hash"], cb["content_hash"])
        self.assertEqual(a.read_bytes(), b.read_bytes())

    def test_restore_leaves_other_games_alone(self):
        self.sb.init_folder_card(self.root, "Mcd001.ps2")
        other = Path(self.root) / "memcards" / "Mcd001.ps2" / "BESLES-11111"
        other.mkdir()
        (other / "x.dat").write_bytes(b"keep me")
        self.sb.restore(unit=Path(self.unit), **self.kw())
        self.assertEqual((other / "x.dat").read_bytes(), b"keep me")

    def test_local_changes_are_refused_not_overwritten(self):
        from rommarmada.sigil import SigilRefusal
        self.sb.init_folder_card(self.root, "Mcd001.ps2")
        self.sb.restore(unit=Path(self.unit), **self.kw(state_out=self.tmp / "s.bin"))
        save = Path(self.root) / "memcards" / "Mcd001.ps2" / "BASLUS-20152" / "save.dat"
        save.write_bytes(b"played since" * 50)
        with self.assertRaises(SigilRefusal) as cm:
            self.sb.restore(unit=Path(self.unit), **self.kw(state_in=self.tmp / "s.bin"))
        self.assertEqual(cm.exception.name, "conflict")
        self.assertEqual(save.read_bytes(), b"played since" * 50)
        self.sb.restore(unit=Path(self.unit), overwrite_local=True, **self.kw(state_in=self.tmp / "s.bin"))
        self.assertNotEqual(save.read_bytes(), b"played since" * 50)

    def test_init_never_replaces_a_file_card(self):
        card = Path(self.root) / "memcards" / "Mcd001.ps2"
        card.parent.mkdir(parents=True)
        card.write_bytes(b"\xff" * 100)
        r = self.sb.init_folder_card(self.root, "Mcd001.ps2")
        self.assertEqual((r["status"], r["kind"]), ("is_file", "file"))
        self.assertEqual(card.read_bytes(), b"\xff" * 100)

    def test_bad_arguments_are_not_a_crash(self):
        from rommarmada.sigil import SavebridgeError
        with self.assertRaises(SavebridgeError):
            self.sb.run("collect")


@unittest.skipUnless(shutil.which("qemu-aarch64-static") and (BIN / "savebridge-aarch64").exists(), "needs qemu-aarch64-static")
class Aarch64Tests(unittest.TestCase):
    """The same scenario through the aarch64 build under qemu: the units must be byte for byte the x86-64 ones."""

    def run_scenario(self, binary, prefix, tmp):
        def run(*a):
            p = subprocess.run([*prefix, str(binary), *a], capture_output=True)
            return json.loads(p.stdout.decode().strip().splitlines()[-1])

        rom = fixtures.write(f"{tmp}/Test Game (USA).iso", fixtures.make_ps2_iso())
        unit = fixtures.write(f"{tmp}/unit.zip", fixtures.make_save_zip())
        root = f"{tmp}/data"
        os.makedirs(root)
        common = ["--layout", "pcsx2_standalone", "--platform", "ps2", "--rom", rom, "--root", root]
        run("init-folder-card", "--root", root, "--path", "Mcd001.ps2")
        r = run("restore", *common, "--unit", unit)
        c = run("collect", *common, "--out", f"{tmp}/out.ps2")
        return r, c, Path(f"{tmp}/out.ps2").read_bytes(), run("version")

    def test_same_results_as_x86_64(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            _, ca, ua, va = self.run_scenario(BIN / "savebridge-x86_64", [], a)
            _, cb, ub, vb = self.run_scenario(BIN / "savebridge-aarch64", ["qemu-aarch64-static"], b)
        self.assertEqual(va["arch"], "x86_64")
        self.assertEqual(vb["arch"], "aarch64")
        self.assertEqual(ua, ub)
        self.assertEqual(ca["content_hash"], cb["content_hash"])
        self.assertEqual(ca["identity_hash"], cb["identity_hash"])


class ArchPickTests(unittest.TestCase):
    def test_arm_first_on_an_unknown_x86_userland(self):
        from rommarmada import sigil
        old = {k: os.environ.pop(k, None) for k in ("ROMM_ARMADA_HELPER_ARCH",)}
        try:
            real = (sigil.platform.machine, sigil._qemu_user_registered, sigil._kernel_says_arm)
            sigil.platform.machine = lambda: "x86_64"
            sigil._qemu_user_registered = lambda: False
            self.assertEqual(sigil.candidate_archs(), ["aarch64", "x86_64"])      # Decky under FEX looks like this
            sigil._qemu_user_registered = lambda: True                              # a PC with qemu-user: don't emulate
            sigil._kernel_says_arm = lambda: False
            self.assertEqual(sigil.candidate_archs(), ["x86_64"])
            sigil.platform.machine = lambda: "aarch64"
            self.assertEqual(sigil.candidate_archs(), ["aarch64"])
        finally:
            sigil.platform.machine, sigil._qemu_user_registered, sigil._kernel_says_arm = real
            for k, v in old.items():
                if v is not None:
                    os.environ[k] = v

    def test_falls_back_when_a_build_cannot_start(self):
        from rommarmada import sigil
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "savebridge-aarch64").write_bytes(b"not an executable")
            shutil.copy(BIN / "savebridge-x86_64", d / "savebridge-x86_64")
            os.environ.pop("ROMM_ARMADA_HELPER_ARCH", None)
            real = (sigil.platform.machine, sigil._qemu_user_registered)
            sigil.platform.machine = lambda: "x86_64"
            sigil._qemu_user_registered = lambda: False
            try:
                sb = sigil.Savebridge(d)
                self.assertEqual(sb.version()["arch"], "x86_64")
                self.assertTrue(any("aarch64" in t for t in sb.tried))
            finally:
                sigil.platform.machine, sigil._qemu_user_registered = real

    def test_a_stripped_executable_bit_is_repaired(self):
        from rommarmada import sigil
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            shutil.copy(BIN / "savebridge-x86_64", d / "savebridge-x86_64")
            os.chmod(d / "savebridge-x86_64", 0o644)                              # what unzipping does
            os.environ["ROMM_ARMADA_HELPER_ARCH"] = "x86_64"
            self.assertEqual(sigil.Savebridge(d).version()["arch"], "x86_64")


class EmulatorTests(Env):
    def test_armsx2_data_dir_follows_xdg(self):
        from rommarmada import emulators
        (self.home / ".config" / "ARMSX2" / "inis").mkdir(parents=True)
        r = emulators.resolve_ps2()
        self.assertEqual(r.profile.id, "armsx2")
        self.assertEqual(r.data_dir, self.home / ".config" / "ARMSX2")
        self.assertEqual((r.why, r.data_dir_exists), ("found", True))

    def test_pcsx2_is_used_when_only_it_exists(self):
        from rommarmada import emulators
        (self.home / ".config" / "PCSX2").mkdir(parents=True)
        self.assertEqual(emulators.resolve_ps2().profile.id, "pcsx2")

    def test_a_fresh_device_defaults_to_armsx2(self):
        from rommarmada import emulators
        r = emulators.resolve_ps2()
        self.assertEqual((r.profile.id, r.data_dir_exists, r.why), ("armsx2", False, "default"))

    def test_setting_wins(self):
        from rommarmada import emulators
        r = emulators.resolve_ps2("pcsx2", str(self.tmp / "custom"))
        self.assertEqual((r.profile.id, r.data_dir, r.why), ("pcsx2", self.tmp / "custom", "setting"))

    def test_card_config_reads_the_ini(self):
        from rommarmada import emulators
        d = self.home / ".config" / "ARMSX2"
        (d / "inis").mkdir(parents=True)
        (d / "inis" / "PCSX2.ini").write_text("[MemoryCards]\nSlot1_Enable = true\nSlot1_Filename = Armada.ps2\n"
                                              "Slot2_Enable = false\nSlot2_Filename = Mcd002.ps2\n[EmuCore]\nMcdFolderAutoManage = false\n")
        (d / "memcards" / "Armada.ps2").mkdir(parents=True)
        cfg = emulators.card_config(d)
        self.assertEqual((cfg.slots[0].name, cfg.slots[0].kind, cfg.slots[0].enabled), ("Armada.ps2", "folder", True))
        self.assertFalse(cfg.slots[1].enabled)
        self.assertFalse(cfg.folder_auto_manage)
        self.assertEqual(emulators.sigil_options(cfg), {"Slot1_Filename": "Armada.ps2"})

    def test_per_game_card_override(self):
        from rommarmada import emulators
        d = self.home / ".config" / "ARMSX2"
        (d / "gamesettings").mkdir(parents=True)
        (d / "gamesettings" / "SLUS-20152_AABBCCDD.ini").write_text("[MemoryCards]\nSlot1_Filename = Racing.ps2\n")
        self.assertEqual(emulators.card_config(d, "SLUS-20152").slots[0].name, "Racing.ps2")
        self.assertEqual(emulators.card_config(d, "SLES-50000").slots[0].name, "Mcd001.ps2")

    def test_moved_memory_card_folder_is_flagged(self):
        from rommarmada import emulators
        d = self.home / ".config" / "ARMSX2"
        (d / "inis").mkdir(parents=True)
        (d / "inis" / "PCSX2.ini").write_text(f"[Folders]\nMemoryCards = {self.tmp}/elsewhere\n")
        self.assertTrue(emulators.card_config(d).custom_dir)

    def test_running_emulator_is_found_by_its_process(self):
        from rommarmada import emulators
        proc = self.tmp / "proc"
        for pid, comm, exe, cmd in ((101, "bash", "/bin/bash", ["bash"]),
                                    (202, "armsx2-qt", "/tmp/.mount_ARMSX2x/usr/bin/armsx2-qt",
                                     ["/tmp/.mount_ARMSX2x/usr/bin/armsx2-qt", "-batch", "--", "/roms/ps2/Game.iso"])):
            d = proc / str(pid)
            d.mkdir(parents=True)
            (d / "comm").write_text(comm + "\n")
            (d / "cmdline").write_bytes(b"\0".join(c.encode() for c in cmd) + b"\0")
            os.symlink(exe, d / "exe")
            (d / "stat").write_text(f"{pid} ({comm}) S " + " ".join(["0"] * 17) + " 12345 0\n")
        found = emulators.find_instances(emulators.all_ps2_process_names(), str(proc))
        self.assertEqual([i.pid for i in found], [202])
        self.assertEqual(found[0].rom_path(), "/roms/ps2/Game.iso")

    def test_the_appimage_as_it_runs_on_the_handheld_and_the_files_it_holds_open(self):
        """The log from a real Armada: the process is called AppRun (the AppImage's entry point, a link to armsx2-qt), the
        game was started from the emulator's menu, so nothing on the command line says which one."""
        from rommarmada import emulators
        proc = self.tmp / "proc"
        d = proc / "3141"
        (d / "fd").mkdir(parents=True)
        (d / "comm").write_text("AppRun\n")
        (d / "cmdline").write_bytes(b"/tmp/.mount_ARMSX2abcdef/AppRun\0")
        os.symlink("/tmp/.mount_ARMSX2abcdef/usr/bin/armsx2-qt", d / "exe")
        for n, target in (("0", "/dev/null"), ("1", "socket:[4242]"), ("2", "/proc/self/stat"), ("3", "anon_inode:[eventfd]"),
                          ("4", "/var/home/armada/ROMs/ps2/Bully.iso"), ("5", "/run/media/mmcblk0p1/ROMs/ps2/Other.iso"),
                          ("6", "/var/home/armada/.config/ARMSX2/logs/emulog.txt (deleted)")):
            os.symlink(target, d / "fd" / n)
        found = emulators.find_instances(emulators.all_ps2_process_names(), str(proc))
        self.assertEqual([i.pid for i in found], [3141])
        self.assertIsNone(found[0].rom_path())
        self.assertEqual(found[0].open_files(), ["/var/home/armada/ROMs/ps2/Bully.iso", "/run/media/mmcblk0p1/ROMs/ps2/Other.iso"])
        self.assertEqual(emulators.Instance(1, "x", "", [], 0.0, str(proc)).open_files(), [], "a process that is gone holds nothing")


class LibraryTests(Env):
    def test_platform_folders(self):
        from rommarmada import library
        self.assertEqual(library.platform_dir_name("esde", {}, "ps", "psx"), "psx")
        self.assertEqual(library.platform_dir_name("esde", {}, "ngc", "ngc"), "gc")
        self.assertEqual(library.platform_dir_name("romm", {}, "ngc", "ngc"), "ngc")
        self.assertEqual(library.platform_dir_name("esde", {"ps2": "PS2 Games"}, "ps2", "ps2"), "PS2 Games")
        self.assertEqual(library.platform_dir_name("esde", {}, "weird-system", "weird"), "weird")

    def test_single_file_and_folder_destinations(self):
        from rommarmada import library
        pid = self.ps2_platform()
        single = self.mock.roms[self.mock.add_rom(pid, "One", [("One.iso", b"x")])]
        multi = self.mock.roms[self.mock.add_rom(pid, "Two", [("Two.m3u", b"a"), ("Two (Disc 1).chd", b"b"), ("sub/Two (Disc 2).chd", b"c")])]
        lay = library.Layout(str(self.home / "ROMs"), "esde", {})
        self.assertEqual(lay.destination(single, single["files"][0]), self.home / "ROMs" / "ps2" / "One.iso")
        dests = {f["file_name"]: lay.destination(multi, f) for f in multi["files"]}
        self.assertEqual(dests["Two.m3u"], self.home / "ROMs" / "ps2" / "Two" / "Two.m3u")
        self.assertEqual(dests["Two (Disc 2).chd"], self.home / "ROMs" / "ps2" / "Two" / "sub" / "Two (Disc 2).chd")
        main = library.choose_main_file(list(dests.values()))
        self.assertEqual(main.name, "Two.m3u")

    def test_hostile_paths_stay_inside(self):
        from rommarmada import library
        lay = library.Layout(str(self.home / "ROMs"), "esde", {})
        rom = {"id": 1, "fs_name": "../../etc/passwd", "platform_slug": "ps2", "platform_fs_slug": "ps2",
               "has_multiple_files": True, "full_path": "roms/ps2/x"}
        f = {"file_name": "../../x", "full_path": "roms/ps2/x/../../../x"}
        self.assertTrue(str(lay.destination(rom, f)).startswith(str(self.home / "ROMs")))


if __name__ == "__main__":
    unittest.main()
