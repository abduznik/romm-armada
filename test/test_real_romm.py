# SPDX-License-Identifier: MIT
"""The plugin against a real RomM server, run in podman. Opt in with ROMM_REAL=1 (it pulls nothing; the images
rommapp/romm and mariadb must already be there). It checks what the mock cannot: that RomM accepts the requests
the plugin makes, and that sigil's content_hash is the one RomM computes for the same bytes.

    ROMM_REAL=1 python3 -m pytest test/test_real_romm.py -x -s
"""
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import tempfile
import time
import unittest
import zipfile
import zlib
from pathlib import Path

import fixtures
from support import BIN, Device, Env, ROOT

REAL = os.environ.get("ROMM_REAL") == "1"
ROMM_IMAGE = os.environ.get("ROMM_IMAGE", "docker.io/rommapp/romm:5.4.1")
DB_IMAGE = os.environ.get("ROMM_DB_IMAGE", "docker.io/library/mariadb:11")
PORT = int(os.environ.get("ROMM_PORT", "18080"))
ALL_SCOPES = ["me.read", "me.write", "roms.read", "platforms.read", "assets.read", "assets.write", "devices.read",
              "devices.write", "roms.user.read", "roms.user.write", "users.read", "users.write", "tasks.run",
              "firmware.read", "firmware.write"]


def sh(*args, check=True, timeout=300):
    return subprocess.run(args, capture_output=True, text=True, check=check, timeout=timeout)


@unittest.skipUnless(REAL, "set ROMM_REAL=1 to run against a real RomM in podman")
class RealRommTests(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        sys_path_fix()
        cls.tag = "rommarmada-real-" + secrets.token_hex(3)
        cls.work = Path(tempfile.mkdtemp(prefix="rommarmada-real-"))
        try:
            cls._boot()
        except BaseException:
            cls.tearDownClass()  # a half-started server must not keep the port for the next run
            raise

    @classmethod
    def _boot(cls):
        cls.lib = cls.work / "library"
        (cls.lib / "roms" / "ps2").mkdir(parents=True)
        (cls.lib / "roms" / "gb").mkdir(parents=True)
        for d in ("assets", "resources", "config", "redis"):
            (cls.work / d).mkdir()
        cls.iso = fixtures.make_ps2_iso("SLUS-20152", extra_bytes=3 * 1024 * 1024)
        fixtures.write(str(cls.lib / "roms" / "ps2" / "Test Game (USA).iso"), cls.iso)
        cls.gb = b"\0" * 0x134 + b"POCKET" + b"\0" * (32768 - 0x134 - 6)         # a Game Boy file: RomM only needs it to be there
        fixtures.write(str(cls.lib / "roms" / "gb" / "Pocket Game (USA).gb"), cls.gb)
        cls.url = f"http://127.0.0.1:{PORT}"
        net, db = cls.tag + "-net", cls.tag + "-db"
        sh("podman", "network", "create", net)
        sh("podman", "run", "-d", "--name", db, "--network", net, "-e", "MARIADB_ROOT_PASSWORD=rootpw",
           "-e", "MARIADB_DATABASE=romm", "-e", "MARIADB_USER=romm", "-e", "MARIADB_PASSWORD=rommpw", DB_IMAGE)
        end = time.time() + 120
        while time.time() < end:
            p = sh("podman", "exec", db, "healthcheck.sh", "--connect", "--innodb_initialized", check=False)
            if p.returncode == 0:
                break
            time.sleep(2)
        else:
            raise RuntimeError("MariaDB did not come up")
        sh("podman", "run", "-d", "--name", cls.tag, "--network", net, "-p", f"127.0.0.1:{PORT}:8080",
           "-e", "DB_HOST=" + db, "-e", "DB_NAME=romm", "-e", "DB_USER=romm", "-e", "DB_PASSWD=rommpw",
           "-e", "ROMM_AUTH_SECRET_KEY=" + secrets.token_hex(32), "-e", "HASHEOUS_API_ENABLED=false",
           "-e", "PLAYMATCH_API_ENABLED=false", "-e", "LIBRETRO_API_ENABLED=false",
           "-v", f"{cls.lib}:/romm/library:Z", "-v", f"{cls.work}/assets:/romm/assets:Z",
           "-v", f"{cls.work}/resources:/romm/resources:Z", "-v", f"{cls.work}/config:/romm/config:Z",
           "-v", f"{cls.work}/redis:/redis-data:Z", ROMM_IMAGE)
        from rommarmada.http import HttpClient, HttpError, NetworkError
        cls.Http, cls.HttpError = HttpClient, HttpError
        anon = HttpClient(cls.url, timeout=5)
        end = time.time() + 240
        while time.time() < end:
            try:
                if anon.request("GET", "/api/heartbeat", retries=0).status == 200:
                    break
            except (HttpError, NetworkError):
                pass
            time.sleep(3)
        else:
            raise RuntimeError("RomM did not come up:\n" + sh("podman", "logs", "--tail", "40", cls.tag, check=False).stdout)
        # The first admin is made the way the web page does it: CSRF protection stays on, the token comes from the cookie.
        import http.cookiejar
        import urllib.request
        jar = http.cookiejar.CookieJar()
        browser = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        browser.open(cls.url + "/api/heartbeat", timeout=10).read()
        csrf = next(c.value for c in jar if c.name == "romm_csrftoken")
        browser.open(urllib.request.Request(
            cls.url + "/api/users", method="POST", headers={"Content-Type": "application/json", "x-csrftoken": csrf},
            data=json.dumps({"username": "admin", "email": "admin@example.com", "password": "Sup3rSecret!pw",
                             "role": "admin"}).encode()), timeout=20).read()
        tok = anon.request("POST", "/api/token", form={"grant_type": "password", "username": "admin",
                                                       "password": "Sup3rSecret!pw", "scope": " ".join(ALL_SCOPES)}).json()
        cls.admin = HttpClient(cls.url, token=tok["access_token"], timeout=30)
        cls.scan()

    @classmethod
    def scan(cls):
        try:
            cls.admin.request("POST", "/api/tasks/scan", json_body={"type": "quick", "apis": []})
        except cls.HttpError as e:
            if e.status not in (404, 405):
                raise
            # RomM before 5.4 starts a scan over its websocket only. The scan function is run inside the container instead,
            # the way its own scheduled rescan calls it.
            code = ("import asyncio\n"
                    "from endpoints.sockets.scan import scan_platforms\n"
                    "from handler.scan_handler import ScanType\n"
                    "asyncio.run(scan_platforms(platform_ids=[], metadata_sources=[], scan_type=ScanType.QUICK))\n")
            sh("podman", "exec", "-w", "/backend", cls.tag, "python", "-c", code, timeout=300)
        end = time.time() + 180
        while time.time() < end:
            page = cls.admin.get_json("/api/roms", params={"limit": 10})
            if page.get("total", 0) >= 2:
                return
            time.sleep(3)
        raise RuntimeError("the scan found nothing:\n" + sh("podman", "logs", "--tail", "60", cls.tag, check=False).stdout)

    @classmethod
    def tearDownClass(cls):
        for name in (cls.tag, cls.tag + "-db"):
            sh("podman", "rm", "-f", "-t", "0", name, check=False)
        sh("podman", "network", "rm", "-f", cls.tag + "-net", check=False)
        shutil.rmtree(cls.work, ignore_errors=True)

    # -- one device on the real server -------------------------------------------------------------------------

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rommarmada-realtest-"))
        self._saved = {k: os.environ.get(k) for k in Device.ENV + ("ROMM_ARMADA_HELPER_ARCH",)}
        os.environ["ROMM_ARMADA_HELPER_ARCH"] = os.environ.get("ROMM_ARMADA_HELPER_ARCH", "x86_64")
        self.events = []

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def device(self, name):
        class _Env:  # the bits Device wants from Env, with the real server
            pass
        e = _Env()
        e.tmp, e.url, e.mock, e.emit = self.tmp, self.url, None, lambda *a: self.events.append(a)
        d = Device.__new__(Device)
        d.env, d.name = e, name
        d.home = self.tmp / f"home-{name}"
        d.home.mkdir()
        d.plugin = self.tmp / f"plugin-{name}"
        with d.active():
            from rommarmada import config, library
            d.settings = config.Settings()
            d.settings.load()
            d.settings.update({"library": {"root": str(d.home / "ROMs")}, "advanced": {"request_timeout": 30},
                               "auth": {"device_name": name}})
            d.manifest = library.Manifest()
        d.sync = None
        return d

    def pair(self, dev, via_code=True):
        """Signs the device in. With a real pairing code from the real server, or (RomM allows 5 code exchanges a
        minute) with the token behind such a code, which is what the typed code turns into anyway."""
        from rommarmada.connect import Connector
        from rommarmada.romm import PAIRING_SCOPES
        made = self.admin.request("POST", "/api/client-tokens", json_body={"name": f"test {dev.name}",
                                                                          "scopes": PAIRING_SCOPES}).json()
        with dev.active():
            if via_code:
                code = self.admin.request("POST", f"/api/client-tokens/{made['id']}/pair").json()["code"]
                done = Connector(dev.settings).pair_with_code(self.url, code)
            else:
                done = Connector(dev.settings).sign_in_token(self.url, made["raw_token"])
        dev.device_id = done["device_id"]
        return done

    def engine(self, dev):
        from rommarmada.http import HttpClient
        from rommarmada.romm import RommClient
        from rommarmada.saves import SaveSync
        from rommarmada.sigil import Savebridge

        def client():
            return RommClient(HttpClient(self.url, token=dev.settings.get("auth.token"), timeout=30))
        dev.sync = SaveSync(dev.settings, dev.manifest, client, Savebridge(BIN), lambda *a: None)
        return dev.sync

    # -- the checks ----------------------------------------------------------------------------------------------

    def test_01_pairing_code_against_real_romm(self):
        d = self.device("pair-a")
        done = self.pair(d)
        self.assertEqual(done["username"], "admin")
        self.assertEqual(done["missing_scopes"], [])
        self.assertTrue(d.settings.signed_in)
        # a code is single use on the real server too
        from rommarmada.connect import ConnectError, Connector
        made = self.admin.request("POST", "/api/client-tokens", json_body={"name": "again", "scopes": [
            "me.read", "roms.read", "platforms.read", "assets.read", "assets.write", "devices.read", "devices.write"]}).json()
        code = self.admin.request("POST", f"/api/client-tokens/{made['id']}/pair").json()["code"]
        with d.active():
            c = Connector(d.settings)
            c.pair_with_code(self.url, code)
            with self.assertRaises(ConnectError) as cm:
                c.pair_with_code(self.url, code)
        self.assertEqual(cm.exception.code, "code_expired")

    def test_01b_a_token_without_permissions_is_explained_by_the_real_server(self):
        d = self.device("pair-weak")
        from rommarmada.connect import ConnectError, Connector
        made = self.admin.request("POST", "/api/client-tokens", json_body={"name": "weak", "scopes": ["me.read"]}).json()
        code = self.admin.request("POST", f"/api/client-tokens/{made['id']}/pair").json()["code"]
        with d.active():
            with self.assertRaises(ConnectError) as cm:
                Connector(d.settings).pair_with_code(self.url, code)
        self.assertEqual(cm.exception.code, "scopes")
        self.assertIn("devices.write", cm.exception.message)
        self.assertFalse(d.settings.signed_in)

    def test_02_device_approval_against_real_romm(self):
        d = self.device("approve-a")
        from rommarmada.connect import Connector
        with d.active():
            c = Connector(d.settings)
            flow = c.start_device_flow(self.url)
            self.assertEqual(len(flow["user_code"]), 9)
            self.admin.request("POST", "/api/auth/device/approve", json_body={
                "user_code": flow["user_code"].replace("-", ""), "approved_scopes": [
                    "me.read", "roms.read", "platforms.read", "assets.read", "assets.write", "devices.read",
                    "devices.write", "roms.user.read", "roms.user.write"]})
            c._flow["next_poll"] = 0
            out = c.poll_device_flow()
        self.assertEqual(out["status"], "approved", out)
        self.assertTrue(d.settings.signed_in)

    def test_03_library_download_and_checksum(self):
        d = self.device("dl")
        self.pair(d, via_code=False)
        from rommarmada.downloads import DownloadManager
        from rommarmada.http import HttpClient
        from rommarmada.romm import RommClient
        with d.active():
            client = RommClient(HttpClient(self.url, token=d.settings.get("auth.token"), timeout=30))
            plats = client.platforms()
            self.assertTrue(any(p["slug"] == "ps2" for p in plats), plats)
            pid = next(p["id"] for p in plats if p["slug"] == "ps2")
            page = client.roms(pid)
            self.assertEqual(page["total"], 1)
            rom = client.rom(page["items"][0]["id"])
            f = rom["files"][0]
            self.assertEqual(f["file_size_bytes"], len(self.iso))
            dm = DownloadManager(d.settings, d.manifest, lambda: client)
            dm.start()
            try:
                dm.enqueue(rom)
                end = time.time() + 60
                while time.time() < end and dm.get(rom["id"])["state"] not in ("done", "error"):
                    time.sleep(0.2)
                job = dm.get(rom["id"])
            finally:
                dm.stop()
            self.assertEqual(job["state"], "done", job)
            dest = d.home / "ROMs" / "ps2" / "Test Game (USA).iso"
            self.assertEqual(dest.read_bytes(), self.iso)
            # RomM's own checksum of the file is the one the plugin verified against
            self.assertEqual(int(f["crc_hash"], 16), zlib.crc32(self.iso) & 0xFFFFFFFF)

    def installed_device(self, name, slug="ps2"):
        d = self.device(name)
        self.pair(d, via_code=False)
        from rommarmada.http import HttpClient
        from rommarmada.romm import RommClient
        from rommarmada import library
        with d.active():
            client = RommClient(HttpClient(self.url, token=d.settings.get("auth.token"), timeout=30))
            pid = next(p["id"] for p in client.platforms() if p["slug"] == slug)
            rom = client.rom(client.roms(pid)["items"][0]["id"])
            lay = library.Layout(d.settings.get("library.root"), "esde", {})
            f = rom["files"][0]
            dest = lay.destination(rom, f)
            fixtures.write(str(dest), self.iso if slug == "ps2" else self.gb)
            d.manifest.put(library.make_entry(rom, lay, [(f, dest)], dest))
            self.engine(d)
        d.rom_id = rom["id"]
        return d

    def test_04_save_folders_round_trip_and_hashes_on_real_romm(self):
        """The default: a zip of the game's save folders in the armadaos slot, hashed by RomM the way the plugin hashes it."""
        a, b = self.installed_device("save-a"), self.installed_device("save-b")
        rid = a.rom_id
        a.play(b"chapter one" * 80)
        a.play(b"someone else's game", serial_folder="BESLES-99999")
        with a.active():
            r = a.sync.sync([rid])[0]
        self.assertEqual(r["status"], "uploaded", r)

        saves = self.admin.get_json("/api/saves", params={"rom_id": rid})
        mine = [s for s in saves if s.get("slot") == "armadaos"]
        self.assertEqual(len(mine), 1, saves)
        s = mine[0]
        self.assertEqual(s["emulator"], "armadaos")
        self.assertRegex(s["file_name"], r"^Test Game \(USA\) \[.*\]\.zip$")
        stored = self.admin.request("GET", f"/api/saves/{s['id']}/content").body
        import io
        names = zipfile.ZipFile(io.BytesIO(stored)).namelist()
        self.assertIn("BASLUS-20152/save.dat", names)
        self.assertFalse([n for n in names if "BESLES" in n], "only this game's folder")
        self.assertLess(len(stored), 20_000)
        # RomM hashes a zip by its entries; the hash the plugin computed for its own zip is the one RomM gave the upload
        with a.active():
            rec = json.loads((a.plugin / "settings" / "state" / "sync" / f"{rid}.json").read_text())
        self.assertEqual(rec["last"]["local_hash"], s["content_hash"])
        self.assertEqual(rec["last"]["format"], "folders")

        # nothing to do the second time, and RomM agrees
        with a.active():
            self.assertEqual(a.sync.sync([rid])[0]["status"], "up_to_date")
        self.assertEqual(len([x for x in self.admin.get_json("/api/saves", params={"rom_id": rid}) if x["slot"] == "armadaos"]), 1)

        # the second handheld gets it, as a folder card
        with b.active():
            r = b.sync.sync([rid])[0]
        self.assertEqual(r["status"], "downloaded", r)
        self.assertTrue(b.card().is_dir())
        self.assertEqual(b.save_bytes(), b"chapter one" * 80)
        with b.active():
            self.assertEqual(b.sync.sync([rid])[0]["status"], "up_to_date")

    def test_04c_retroarch_saves_on_real_romm(self):
        """A Game Boy game's save, as RetroArch keeps it: the .srm as it is, a zip when there is a clock too, hashed by RomM."""
        a, b = self.installed_device("ra-a", "gb"), self.installed_device("ra-b", "gb")
        rid = a.rom_id
        for d in (a, b):
            folder = d.home / ".config" / "retroarch"
            (folder / "saves").mkdir(parents=True)
            (folder / "retroarch.cfg").write_text(f'savefile_directory = "{folder / "saves"}"\n')
        saves = a.home / ".config" / "retroarch" / "saves"
        data = os.urandom(8192)
        (saves / "Pocket Game (USA).srm").write_bytes(data)
        with a.active():
            r = a.sync.sync([rid])[0]
        self.assertEqual(r["status"], "uploaded", r)
        (s,) = [x for x in self.admin.get_json("/api/saves", params={"rom_id": rid}) if x.get("slot") == "armadaos"]
        self.assertEqual((s["emulator"], s["content_hash"]), ("armadaos", hashlib.md5(data).hexdigest()))
        self.assertRegex(s["file_name"], r"^Pocket Game \(USA\) \[.*\]\.srm$")
        self.assertEqual(self.admin.request("GET", f"/api/saves/{s['id']}/content").body, data)
        with a.active():
            self.assertEqual(a.sync.sync([rid])[0]["status"], "up_to_date")

        # with a clock: a flat zip, whose hash RomM computes from its entries, the way the plugin did
        time.sleep(1.1)
        (saves / "Pocket Game (USA).srm").write_bytes(data + b"more")
        (saves / "Pocket Game (USA).rtc").write_bytes(b"\x07" * 48)
        with a.active():
            r = a.sync.sync([rid])[0]
        self.assertEqual(r["status"], "uploaded", r)
        newest = max((x for x in self.admin.get_json("/api/saves", params={"rom_id": rid}) if x.get("slot") == "armadaos"),
                     key=lambda x: x["updated_at"])
        self.assertRegex(newest["file_name"], r"^Pocket Game \(USA\)\.srm \[.*\]\.zip$")
        rec = json.loads((a.plugin / "settings" / "state" / "sync" / f"{rid}.json").read_text())
        self.assertEqual((rec["last"]["local_hash"], rec["last"]["format"]), (newest["content_hash"], "file"))

        with b.active():
            r = b.sync.sync([rid])[0]
        self.assertEqual(r["status"], "downloaded", r)
        mine = b.home / ".config" / "retroarch" / "saves"
        self.assertEqual((mine / "Pocket Game (USA).srm").read_bytes(), data + b"more")
        self.assertEqual((mine / "Pocket Game (USA).rtc").read_bytes(), b"\x07" * 48)
        with b.active():
            self.assertEqual(b.sync.sync([rid])[0]["status"], "up_to_date")

    def test_04d_save_states_on_real_romm(self):
        """A PS2 save state goes to RomM's states under its own name and emulator, replaces itself, and reaches a second device."""
        a, b = self.installed_device("st-a"), self.installed_device("st-b")
        rid = a.rom_id
        name = "SLUS-20152 (12345678).01.p2s"
        folder = a.home / ".config" / "ARMSX2" / "sstates"
        folder.mkdir(parents=True)
        (folder / name).write_bytes(os.urandom(70_000))
        with a.active():
            r = a.sync.sync([rid])[0]
        self.assertIn("1 save state uploaded", r["message"], r)
        (st,) = self.admin.get_json("/api/states", params={"rom_id": rid})
        self.assertEqual((st["file_name"], st["emulator"]), (name, "armsx2"))
        self.assertEqual(self.admin.request("GET", f"/api/states/{st['id']}/content").body, (folder / name).read_bytes())
        time.sleep(1.1)
        (folder / name).write_bytes(os.urandom(70_000))
        with a.active():
            a.sync.sync([rid])
        (st2,) = self.admin.get_json("/api/states", params={"rom_id": rid})
        self.assertEqual(st2["id"], st["id"], "the same name replaces the state")
        with b.active():
            r = b.sync.sync([rid])[0]
        self.assertIn("1 save state downloaded", r["message"], r)
        self.assertEqual((b.home / ".config" / "ARMSX2" / "sstates" / name).read_bytes(), (folder / name).read_bytes())

    def test_04b_card_image_format_on_real_romm(self):
        """sigil's per-game card image, for servers that keep units (RomM 5.5 and newer) or when asked for."""
        a, b = self.installed_device("card-a"), self.installed_device("card-b")
        for d in (a, b):
            d.settings.update({"saves": {"format": "card", "slot": "autosave"}})
        rid = a.rom_id
        a.play(b"card image" * 80)
        with a.active():
            r = a.sync.sync([rid])[0]
            # the save the earlier test left in the armadaos slot is a different save: this device has never synced, so it asks
            self.assertEqual((r["status"], r["conflict"]["reason"]), ("conflict", "first_contact"), r)
            r = a.sync.sync([rid], resolutions={rid: "device"})[0]
        self.assertEqual(r["status"], "uploaded", r)
        mine = [s for s in self.admin.get_json("/api/saves", params={"rom_id": rid}) if s.get("slot") == "autosave"]
        self.assertEqual(len(mine), 1, mine)
        stored = self.admin.request("GET", f"/api/saves/{mine[0]['id']}/content").body
        self.assertEqual(len(stored), 8650752)
        self.assertEqual(hashlib.md5(stored).hexdigest(), mine[0]["content_hash"])
        with b.active():
            r = b.sync.sync([rid])[0]
        self.assertEqual(r["status"], "downloaded", r)
        self.assertEqual(b.save_bytes(), b"card image" * 80)

    def test_05_conflicts_on_real_romm(self):
        a, b = self.installed_device("conf-a"), self.installed_device("conf-b")
        rid = a.rom_id
        # this game already has a save on the server from the earlier test, so b starts as a first contact with a save
        b.play(b"b's own game" * 50)
        with b.active():
            r = b.sync.sync([rid])[0]
        self.assertEqual(r["status"], "conflict", r)
        self.assertEqual(r["conflict"]["reason"], "first_contact")
        self.assertEqual(b.save_bytes(), b"b's own game" * 50)
        with b.active():
            r = b.sync.sync([rid], resolutions={rid: "keep_both"})[0]
        self.assertEqual(r["status"], "downloaded", r)
        slots = {s["slot"] for s in self.admin.get_json("/api/saves", params={"rom_id": rid})}
        self.assertTrue(any(x.startswith("device-") for x in slots), slots)
        with b.active():
            self.assertEqual(b.sync.sync([rid])[0]["status"], "up_to_date")

    def test_06_play_time_reaches_real_romm(self):
        a = self.installed_device("play-a")
        with a.active():
            from rommarmada.http import HttpClient
            from rommarmada.romm import RommClient
            client = RommClient(HttpClient(self.url, token=a.settings.get("auth.token"), timeout=30))
            now = time.time()
            from rommarmada.util import ts_to_iso
            out = client.play_sessions(a.settings.get("auth.device_id"), [{
                "rom_id": a.rom_id, "save_slot": "autosave", "start_time": ts_to_iso(now - 90).split(".")[0] + "Z",
                "end_time": ts_to_iso(now).split(".")[0] + "Z", "duration_ms": 90_000}])
        self.assertEqual(out["created_count"], 1)

    def test_06b_the_latest_save_is_pulled_from_any_slot_on_real_romm(self):
        """A save Freegosy made, in its own slot and shape, is what a fresh handheld gets: the newest save wins."""
        import io
        from rommarmada.http import multipart
        from rommarmada.savefolders import romm_content_hash
        from test_folders import freegosy_zip
        d = self.installed_device("anyslot")
        rid = d.rom_id
        data = freegosy_zip("BASLUS-20152AC04", {"icon.sys": b"PS2D" + b"\0" * 60 + b"x", "save.dat": b"from the pc" * 50})
        body, ctype = multipart(None, [("saveFile", "Test Game (USA).zip", data, "application/zip")])
        self.admin.request("POST", "/api/saves", params={"rom_id": rid, "emulator": "freegosy", "slot": "freegosy"}, data=body,
                           headers={"Content-Type": ctype}, timeout=60, retries=0)
        theirs = [s for s in self.admin.get_json("/api/saves", params={"rom_id": rid}) if s["slot"] == "freegosy"]
        self.assertEqual(len(theirs), 1)
        self.assertEqual(theirs[0]["content_hash"], romm_content_hash(data), "the real server hashes a zip the way the plugin does")
        with d.active():
            r = d.sync.sync([rid])[0]
        self.assertEqual(r["status"], "downloaded", r)
        card = d.card()
        self.assertEqual((card / "BASLUS-20152AC04" / "save.dat").read_bytes(), b"from the pc" * 50)
        self.assertFalse(any(p.name == "freegosy_sync.txt" for p in card.rglob("*")))
        with d.active():
            self.assertEqual(d.sync.sync([rid])[0]["status"], "up_to_date")
        # playing here goes to the plugin's own slot, and the other slot is left as it was
        time.sleep(0.05)
        (card / "BASLUS-20152AC04" / "save.dat").write_bytes(b"continued on the handheld" * 50)
        with d.active():
            r = d.sync.sync([rid])[0]
        self.assertEqual(r["status"], "uploaded", r)
        now = self.admin.get_json("/api/saves", params={"rom_id": rid})
        self.assertEqual(len([s for s in now if s["slot"] == "freegosy"]), 1)
        newest = max((s for s in now if s["slot"] == "armadaos"), key=lambda s: s["updated_at"])
        zf = zipfile.ZipFile(io.BytesIO(self.admin.request("GET", f"/api/saves/{newest['id']}/content").body))
        self.assertEqual(zf.read("BASLUS-20152AC04/save.dat"), b"continued on the handheld" * 50)

    def test_07_bios_files_from_real_romm_firmware(self):
        """The firmware list and download RomM really serves, into the emulator's BIOS folder; and the real server's
        answer to a sign-in that may not read firmware."""
        from rommarmada.connect import Connector
        from rommarmada.http import multipart
        from rommarmada.romm import PAIRING_SCOPES
        from rommarmada.service import Service
        from rommarmada.sigil import Savebridge
        plats = self.admin.get_json("/api/platforms")
        pid = next(p["id"] for p in plats if p["slug"] == "ps2")
        files = {}
        for name, seed in (("scph70012.bin", b"seven"), ("scph39001.bin", b"three")):
            data = bytearray((hashlib.sha256(seed).digest() * (4 * 1024 * 1024 // 32))[:4 * 1024 * 1024])
            data[10048:10064] = b"RESET" + b"\0" * 11            # the ROM directory entry a real BIOS has there
            files[name] = bytes(data)
            body, ctype = multipart(None, [("files", name, files[name], "application/octet-stream")])
            self.admin.request("POST", "/api/firmware", params={"platform_id": pid}, data=body,
                               headers={"Content-Type": ctype}, timeout=60, retries=0)

        d = self.device("bios-a")
        done = self.pair(d, via_code=False)
        self.assertEqual(done["missing_scopes"], [], "the real server accepts the firmware probe the plugin makes")
        (self.tmp / "proc").mkdir()
        with d.active():
            svc = Service(emit=lambda *a: None, bridge=Savebridge(BIN), settings=d.settings, proc_root=str(self.tmp / "proc"))
            st = svc.bios_status()
            self.assertTrue(st["remote"]["ok"], st)
            self.assertEqual(sorted(i["name"] for i in st["remote"]["items"]), sorted(files))
            self.assertEqual({i["state"] for i in st["remote"]["items"]}, {"missing"})
            self.assertEqual(st["dir"], str(d.home / ".config" / "ARMSX2" / "bios"))
            out = svc.bios_install()
            self.assertTrue(out["ok"], out)
            for name, data in files.items():
                self.assertEqual((Path(st["dir"]) / name).read_bytes(), data, name)
            self.assertEqual(sorted(p.name for p in Path(st["dir"]).iterdir()), sorted(files), "no half-finished files")
            self.assertTrue(svc.overview()["emulator"]["bios"]["has_bios"])
            again = svc.bios_install()
            self.assertEqual((again["report"]["installed"], sorted(again["report"]["skipped"])), ([], sorted(files)))

        # RomM's hashes for the stored files are the ones the plugin verified against
        listed = {f["file_name"]: f for f in self.admin.get_json("/api/firmware", params={"platform_id": pid})}
        for name, data in files.items():
            self.assertEqual(listed[name]["md5_hash"], hashlib.md5(data).hexdigest())

        # a token that may not read firmware is told so, by the real server
        weak = self.admin.request("POST", "/api/client-tokens", json_body={
            "name": "no firmware", "scopes": [x for x in PAIRING_SCOPES if x != "firmware.read"]}).json()
        w = self.device("bios-weak")
        with w.active():
            signed = Connector(w.settings).sign_in_token(self.url, weak["raw_token"])
            self.assertEqual(signed["missing_scopes"], ["firmware.read"])
            svc2 = Service(emit=lambda *a: None, bridge=Savebridge(BIN), settings=w.settings, proc_root=str(self.tmp / "proc"))
            refused = svc2.bios_status()["remote"]
        self.assertFalse(refused["ok"])
        self.assertEqual(refused["reason"], "forbidden")
        self.assertIn("pair again", refused["message"])


def sys_path_fix():
    import sys
    sys.path.insert(0, str(ROOT / "py_modules"))


if __name__ == "__main__":
    unittest.main()
