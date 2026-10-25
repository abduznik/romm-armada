# SPDX-License-Identifier: MIT
"""RomM Armada: Decky entry point.

Everything of substance lives in py_modules/rommarmada. This file only adapts it to Decky: it starts the
service, runs each call the panel makes in a worker thread (Decky's loop must never wait on the network or on
a memory card), and turns the failures a user can act on into {"ok": false, "error", "message"} answers.
"""
import asyncio
import os
import sys

import decky

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "py_modules"))

from rommarmada import log as rlog  # noqa: E402

rlog.set_logger(decky.logger)

from rommarmada.connect import ConnectError  # noqa: E402
from rommarmada.romm import RommError, describe  # noqa: E402
from rommarmada.service import Service  # noqa: E402
from rommarmada.sigil import SavebridgeError, SigilRefusal  # noqa: E402


class Plugin:
    svc: Service
    loop: asyncio.AbstractEventLoop

    # -- lifecycle -------------------------------------------------------------------------------------

    async def _main(self):
        self.loop = asyncio.get_event_loop()
        self.svc = Service(emit=self._emit)
        await asyncio.to_thread(self.svc.start)
        decky.logger.info("RomM Armada is running")

    async def _unload(self):
        if getattr(self, "svc", None):
            await asyncio.to_thread(self.svc.stop)
        decky.logger.info("RomM Armada stopped")

    async def _uninstall(self):
        decky.logger.info("RomM Armada removed; its settings and downloaded games stay where they are")

    def _emit(self, event: str, payload: dict) -> None:
        """Called from worker threads."""
        asyncio.run_coroutine_threadsafe(decky.emit(event, payload), self.loop)

    # -- plumbing --------------------------------------------------------------------------------------

    async def _run(self, fn, *args):
        try:
            out = await asyncio.to_thread(fn, *args)
        except ConnectError as e:
            return {"ok": False, "error": e.code, "message": e.message}
        except RommError as e:
            if e.kind == "auth":
                self._emit("auth_required", {})
            return {"ok": False, "error": e.kind, "message": describe(e)}
        except SigilRefusal as e:
            return {"ok": False, "error": e.name, "message": str(e)}
        except SavebridgeError as e:
            return {"ok": False, "error": "helper", "message": str(e)}
        except ValueError as e:
            return {"ok": False, "error": "invalid", "message": str(e)}
        except Exception as e:  # noqa: BLE001
            decky.logger.exception("call failed")
            return {"ok": False, "error": "internal", "message": f"{e.__class__.__name__}: {e}"}
        if isinstance(out, dict):
            out.setdefault("ok", True)
            return out
        return {"ok": True, "items": out}

    # -- overview and settings --------------------------------------------------------------------------

    async def overview(self):
        return await self._run(self.svc.overview)

    async def diagnostics(self):
        return await self._run(self.svc.diagnostics)

    async def settings_get(self):
        return await self._run(self.svc.get_settings)

    async def settings_set(self, patch):
        return await self._run(self.svc.update_settings, patch)

    async def check_location(self, path):
        return await self._run(self.svc.check_location, path)

    async def volumes(self):
        return await self._run(self.svc.volumes)

    # -- connecting ------------------------------------------------------------------------------------------

    async def connect_probe(self, url, verify_tls=True):
        return await self._run(self.svc.connector.probe, url, bool(verify_tls))

    async def connect_code(self, url, code, verify_tls=True):
        out = await self._run(self.svc.connector.pair_with_code, url, code, bool(verify_tls))
        if out.get("ok"):
            self.svc.after_connect()
        return out

    async def connect_device_start(self, url, verify_tls=True):
        return await self._run(self.svc.connector.start_device_flow, url, bool(verify_tls))

    async def connect_device_poll(self):
        out = await self._run(self.svc.connector.poll_device_flow)
        if out.get("status") == "approved":
            self.svc.after_connect()
        return out

    async def connect_device_cancel(self):
        return await self._run(self.svc.connector.cancel_device_flow)

    async def connect_token(self, url, token, verify_tls=True):
        out = await self._run(self.svc.connector.sign_in_token, url, token, bool(verify_tls))
        if out.get("ok"):
            self.svc.after_connect()
        return out

    async def connect_password(self, url, username, password, verify_tls=True):
        out = await self._run(self.svc.connector.sign_in_password, url, username, password, bool(verify_tls))
        if out.get("ok"):
            self.svc.after_connect()
        return out

    async def disconnect(self):
        return await self._run(self.svc.disconnect)

    # -- library ---------------------------------------------------------------------------------------------

    async def platforms(self):
        return await self._run(self.svc.platforms)

    async def roms(self, platform_id, search="", offset=0, limit=40):
        return await self._run(self.svc.roms, platform_id, search, int(offset), int(limit))

    async def installed(self, search="", offset=0, limit=40):
        return await self._run(self.svc.installed_games, search, int(offset), int(limit))

    async def rom(self, rom_id):
        return await self._run(self.svc.rom, int(rom_id))

    async def install(self, rom_id):
        return await self._run(self.svc.install, int(rom_id))

    async def uninstall(self, rom_id):
        return await self._run(self.svc.uninstall, int(rom_id))

    # -- downloads -------------------------------------------------------------------------------------------

    async def downloads(self):
        return await self._run(self.svc.downloads.status)

    async def download_pause(self, rom_id):
        return await self._run(self.svc.downloads.pause, int(rom_id))

    async def download_resume(self, rom_id):
        return await self._run(self.svc.downloads.resume, int(rom_id))

    async def download_cancel(self, rom_id):
        return await self._run(self.svc.downloads.cancel, int(rom_id))

    async def downloads_clear(self):
        return await self._run(self.svc.downloads.clear)

    # -- saves ------------------------------------------------------------------------------------------------

    async def sync_game(self, rom_id, resolution=None):
        return await self._run(self.svc.sync_game, int(rom_id), resolution)

    async def sync_all(self):
        return await self._run(self.svc.sync_all)

    async def prepare_cards(self):
        return await self._run(self.svc.prepare_cards)

    async def game_sync_enabled(self, rom_id, enabled):
        return await self._run(self.svc.set_game_sync, int(rom_id), bool(enabled))

    async def game_sync_reset(self, rom_id):
        return await self._run(self.svc.forget_game_sync, int(rom_id))

    async def conflicts(self):
        return await self._run(self.svc.conflicts)

    async def backups(self, rom_id):
        return await self._run(self.svc.backups, int(rom_id))

    async def backup_restore(self, rom_id, name):
        return await self._run(self.svc.restore_backup, int(rom_id), str(name))
