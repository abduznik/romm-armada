# SPDX-License-Identifier: MIT
"""Runs the savebridge helper, the C program that talks to argosy-sigil.

Two static builds ship in bin/. The aarch64 one is tried first: on Armada, Decky itself runs under
FEX as an x86-64 program, so `platform.machine()` says x86_64 even though the handheld is Arm64, and a
static aarch64 executable still starts natively from there. Where it cannot start (a Steam Deck, a PC)
the exec fails at once and the x86-64 build is used. Both are probed with `version` before anything
depends on them, and the answer is remembered.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any

from . import log, paths

ARCHS = ("aarch64", "x86_64")


class SavebridgeError(Exception):
    """The helper is missing, would not start, or broke. Not a refusal by sigil."""


class SigilRefusal(Exception):
    """sigil said no: `name` is one of conflict, damaged, no_space, ambiguous, uncollected, not_found, ..."""

    def __init__(self, doc: dict) -> None:
        self.code = int(doc.get("code", 0))
        self.name = str(doc.get("error", "error"))
        self.message = str(doc.get("message", self.name))
        self.problem = str(doc.get("problem", ""))
        self.blocks_short = int(doc.get("blocks_short", 0) or 0)
        self.profiles = doc.get("profiles") or []
        super().__init__(f"{self.name}: {self.message}" + (f" ({self.problem})" if self.problem else ""))


def _kernel_says_arm() -> bool:
    rel = platform.release().lower()
    return any(t in rel for t in ("aarch64", "arm64", "armada"))


def _qemu_user_registered() -> bool:
    try:
        return any(n.startswith("qemu-aarch64") for n in os.listdir("/proc/sys/fs/binfmt_misc"))
    except OSError:
        return False


def candidate_archs() -> list[str]:
    forced = os.environ.get("ROMM_ARMADA_HELPER_ARCH", "").strip()
    if forced:
        return [forced]
    machine = platform.machine().lower()
    if machine in ("aarch64", "arm64"):
        return ["aarch64"]
    # An x86-64 userland: a PC, a Steam Deck, or Decky under FEX on an Arm64 handheld. A PC that has
    # qemu-user registered would run the aarch64 build through emulation, which is not what anyone wants.
    if _qemu_user_registered() and not _kernel_says_arm():
        return ["x86_64"]
    return ["aarch64", "x86_64"]


def ensure_executable(path: Path) -> Path:
    """Zip extraction drops the executable bit. Decky hands a non-root plugin its own files, so a chmod
    works; failing that the helper is copied under the runtime folder and run from there."""
    if os.access(path, os.X_OK):
        return path
    try:
        os.chmod(path, 0o755)
        if os.access(path, os.X_OK):
            return path
    except OSError:
        pass
    dest_dir = paths.runtime_dir() / "bin"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / path.name
    if not dest.exists() or dest.stat().st_size != path.stat().st_size:
        shutil.copyfile(path, dest)
    os.chmod(dest, 0o755)
    return dest


class Savebridge:
    def __init__(self, bin_dir: Path | None = None) -> None:
        self.bin_dir = Path(bin_dir) if bin_dir else paths.helper_dir()
        self.path: Path | None = None
        self.info: dict = {}
        self.tried: list[str] = []
        self._lock = threading.Lock()

    # -- finding a helper that runs -------------------------------------------------------

    def _probe(self, path: Path) -> dict | None:
        try:
            p = subprocess.run([str(path), "version"], capture_output=True, timeout=20)
        except (OSError, subprocess.SubprocessError) as e:
            self.tried.append(f"{path.name}: {e}")
            return None
        if p.returncode != 0:
            self.tried.append(f"{path.name}: exit {p.returncode}")
            return None
        try:
            doc = json.loads(p.stdout.decode("utf-8", "replace").strip().splitlines()[-1])
        except (ValueError, IndexError):
            self.tried.append(f"{path.name}: no answer")
            return None
        return doc if doc.get("ok") else None

    def resolve(self) -> Path:
        with self._lock:
            if self.path:
                return self.path
            for arch in candidate_archs():
                p = self.bin_dir / f"savebridge-{arch}"
                if not p.exists():
                    self.tried.append(f"{p.name}: not in the plugin")
                    continue
                try:
                    exe = ensure_executable(p)
                except OSError as e:
                    self.tried.append(f"{p.name}: {e}")
                    continue
                doc = self._probe(exe)
                if doc:
                    self.path, self.info = exe, doc
                    log.info("savebridge %s (%s), sigil %s", doc.get("helper"), doc.get("arch"), doc.get("sigil_pin"))
                    return exe
            raise SavebridgeError("No save helper runs on this device: " + "; ".join(self.tried))

    @property
    def arch(self) -> str:
        self.resolve()
        return str(self.info.get("arch", "?"))

    # -- running it -------------------------------------------------------------------------

    def run(self, *args: str, timeout: float = 120.0) -> dict:
        exe = self.resolve()
        try:
            p = subprocess.run([str(exe), *args], capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise SavebridgeError(f"The save helper took longer than {int(timeout)} seconds") from None
        except OSError as e:
            raise SavebridgeError(f"The save helper would not start: {e}") from None
        out = p.stdout.decode("utf-8", "replace").strip()
        doc: dict[str, Any] | None = None
        if out:
            try:
                doc = json.loads(out.splitlines()[-1])
            except ValueError:
                doc = None
        if doc is None:
            err = p.stderr.decode("utf-8", "replace").strip()[-300:]
            raise SavebridgeError(f"The save helper gave no answer (exit {p.returncode}) {err}".strip())
        if not doc.get("ok"):
            if doc.get("code"):  # sigil's own error codes are the non-zero ones; the helper's own are 0
                raise SigilRefusal(doc)
            raise SavebridgeError(str(doc.get("message") or doc.get("error") or "the save helper failed"))
        return doc

    # -- commands ---------------------------------------------------------------------------

    def version(self) -> dict:
        self.resolve()
        return dict(self.info)

    @staticmethod
    def _game_args(rom: str | None, identity: dict | None, platform_slug: str | None) -> list[str]:
        args: list[str] = []
        if rom and os.path.exists(rom):
            args += ["--rom", rom]
            if platform_slug:
                args += ["--platform", platform_slug]
            return args
        identity = identity or {}
        slug = platform_slug or identity.get("platform")
        if slug:
            args += ["--platform", slug]
        for key, flag in (("title_id", "--title-id"), ("save_id", "--save-id"), ("raw_serial", "--raw-serial"),
                          ("n64_header", "--n64-header"), ("n64_md5", "--n64-md5"), ("n64_md5_n64", "--n64-md5-n64")):
            if identity.get(key):
                args += [flag, str(identity[key])]
        if identity.get("features"):
            args += ["--features", str(int(identity["features"]))]
        return args

    def identify(self, rom: str, platform_slug: str | None = None) -> dict:
        args = ["identify", "--rom", rom]
        if platform_slug:
            args += ["--platform", platform_slug]
        return self.run(*args)

    def layouts(self, platform_slug: str | None = None) -> list[dict]:
        args = ["layouts"] + (["--platform", platform_slug] if platform_slug else [])
        return self.run(*args)["layouts"]

    def locate(self, *, layout: str, root: str, rom: str | None, identity: dict | None, platform_slug: str | None,
               options: dict[str, str] | None = None, hash: bool = False) -> dict:
        args = ["locate", "--layout", layout, "--root", root, *self._game_args(rom, identity, platform_slug)]
        for k, v in (options or {}).items():
            args += ["--option", f"{k}={v}"]
        if hash:
            args.append("--hash")
        return self.run(*args)

    def _sync(self, op: str, *, layout: str, root: str, rom: str | None, identity: dict | None, platform_slug: str | None,
              content: str | None, options: dict[str, str] | None, game_ids: list[str] | None, state_in: Path | None,
              state_out: Path | None, unit: Path | None, mode: str, overwrite_local: bool, repair: bool) -> dict:
        args = [op, "--layout", layout, "--root", root, "--mode", mode, *self._game_args(rom, identity, platform_slug)]
        if content:
            args += ["--content", content]
        for k, v in (options or {}).items():
            args += ["--option", f"{k}={v}"]
        for gid in game_ids or []:
            args += ["--game-id", gid]
        if state_in and Path(state_in).exists():
            args += ["--state-in", str(state_in)]
        if state_out:
            args += ["--state-out", str(state_out)]
        if unit is not None:
            args += ["--unit" if op == "restore" else "--out", str(unit)]
        if overwrite_local:
            args.append("--overwrite-local")
        if repair:
            args.append("--repair")
        return self.run(*args)

    def collect(self, *, layout: str, root: str, out: Path, rom: str | None = None, identity: dict | None = None,
                platform_slug: str | None = None, content: str | None = None, options: dict[str, str] | None = None,
                game_ids: list[str] | None = None, state_in: Path | None = None, state_out: Path | None = None,
                mode: str = "unmanaged", repair: bool = False) -> dict:
        try:
            Path(out).unlink()  # a unit left over from an earlier run must not pass for this one
        except FileNotFoundError:
            pass
        return self._sync("collect", layout=layout, root=root, rom=rom, identity=identity, platform_slug=platform_slug,
                          content=content, options=options, game_ids=game_ids, state_in=state_in, state_out=state_out,
                          unit=out, mode=mode, overwrite_local=False, repair=repair)

    def restore(self, *, layout: str, root: str, unit: Path, rom: str | None = None, identity: dict | None = None,
                platform_slug: str | None = None, content: str | None = None, options: dict[str, str] | None = None,
                game_ids: list[str] | None = None, state_in: Path | None = None, state_out: Path | None = None,
                mode: str = "unmanaged", overwrite_local: bool = False, repair: bool = False) -> dict:
        return self._sync("restore", layout=layout, root=root, rom=rom, identity=identity, platform_slug=platform_slug,
                          content=content, options=options, game_ids=game_ids, state_in=state_in, state_out=state_out,
                          unit=unit, mode=mode, overwrite_local=overwrite_local, repair=repair)

    def init_folder_card(self, root: str, name: str, memcards: str = "memcards") -> dict:
        return self.run("init-folder-card", "--root", root, "--path", name, "--layout", memcards)

    def card_list(self, path: str) -> dict:
        return self.run("card-list", "--path", path)
