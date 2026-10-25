#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Loads the plugin the way Decky does and prints what the panel would get on its first screen.

    python3 scripts/smoke_install.py RomM-Armada-0.1.0.zip          # unzip like Decky (no permission bits), then load
    python3 scripts/smoke_install.py ~/homebrew/plugins/romm-armada --real-home   # an installed copy, on the handheld

It uses a stand-in for Decky's `decky` module and, unless --real-home is given, a throwaway home folder. Nothing is
installed or changed. On the handheld it answers the first question to ask: which save helper runs there.
"""
import argparse
import asyncio
import json
import logging
import os
import sys
import tempfile
import types
import zipfile
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("plugin", help="a plugin zip, or an unpacked/installed plugin folder")
    ap.add_argument("--real-home", action="store_true", help="use this user's real home instead of a throwaway one")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="romm-armada-smoke-"))
    src = Path(args.plugin).expanduser()
    if src.is_file():
        with zipfile.ZipFile(src) as z:
            z.extractall(tmp / "plugins")         # like Decky: extractall() drops the executable bit
        roots = [p for p in (tmp / "plugins").iterdir() if (p / "plugin.json").exists()]
        if len(roots) != 1:
            print("the zip must hold one folder with a plugin.json", file=sys.stderr)
            return 2
        plugin_dir = roots[0]
    else:
        plugin_dir = src
    name = plugin_dir.name
    home = Path.home() if args.real_home else tmp / "home"
    home.mkdir(parents=True, exist_ok=True)
    brew = home / "homebrew" if not args.real_home else home / "homebrew"
    env = {
        "HOME": str(home), "USER": os.environ.get("USER", "user"), "DECKY_USER_HOME": str(home), "DECKY_HOME": str(brew),
        "DECKY_PLUGIN_DIR": str(plugin_dir), "DECKY_PLUGIN_NAME": "RomM Armada",
        "DECKY_PLUGIN_SETTINGS_DIR": str(brew / "settings" / name), "DECKY_PLUGIN_RUNTIME_DIR": str(brew / "data" / name),
        "DECKY_PLUGIN_LOG_DIR": str(brew / "logs" / name),
    }
    os.environ.update(env)
    for key in ("ROMM_ARMADA_HOME",):
        os.environ.pop(key, None)
    for d in ("DECKY_PLUGIN_SETTINGS_DIR", "DECKY_PLUGIN_RUNTIME_DIR", "DECKY_PLUGIN_LOG_DIR"):
        Path(env[d]).mkdir(parents=True, exist_ok=True)

    decky = types.ModuleType("decky")
    decky.logger = logging.getLogger("decky")
    events = []

    async def emit(event, *a):
        events.append(event)
    decky.emit = emit
    sys.modules["decky"] = decky
    sys.path.insert(0, str(plugin_dir))
    sys.path.insert(0, str(plugin_dir / "py_modules"))
    import main as plugin_main  # noqa: E402  (the plugin's own main.py)

    exe_before = {p.name: oct(p.stat().st_mode & 0o777) for p in (plugin_dir / "bin").glob("savebridge-*")}

    async def go() -> dict:
        p = plugin_main.Plugin()
        await p._main()
        try:
            ov = await p.overview()
            dg = await p.diagnostics()
            st = await p.settings_get()
        finally:
            await p._unload()
        return {"overview": ov, "diagnostics": dg, "settings_ok": st.get("ok"), "card_type": st.get("saves", {}).get("card_type")}

    out = asyncio.run(go())
    ov, dg = out["overview"], out["diagnostics"]
    print(json.dumps({
        "plugin_dir": str(plugin_dir), "helper_modes_before": exe_before,
        "helper": dg.get("helper"), "helper_tried": dg.get("helper_tried"),
        "emulator": {k: ov["emulator"][k] for k in ("name", "data_dir", "data_dir_exists", "executable", "found_by")},
        "card": ov["emulator"]["card"], "library_root": ov["library"]["root"], "default_card_type": out["card_type"],
        "host": dg.get("host"),
    }, indent=2))
    ok = bool(dg.get("helper", {}).get("ok")) and bool(ov.get("ok"))
    print("\nhelper runs:", "yes" if ok else "NO")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
