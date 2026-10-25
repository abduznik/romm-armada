# Decky, Armada OS and emulator notes

Facts about the places this plugin runs in, found by reading their code and by looking at a real handheld.

## Running as a plugin on Armada OS

- **Decky's Python is x86-64 under FEX.** `platform.machine()` says `x86_64` on an Arm64 device. The helper picker
  therefore tries the aarch64 build first and falls back to x86-64, probes each with `savebridge version` before
  relying on it, and remembers the answer. On a PC that has qemu-user registered it picks x86-64, so a test run never
  emulates by accident. `ROMM_ARMADA_HELPER_ARCH` forces one. Settings → About shows which one runs.
- **No `root` flag.** `plugin.json` has `"flags": []`. Decky hands a non-root plugin's files to the normal user, so
  everything the plugin downloads or writes belongs to that user, and the backend may chmod its own helper.
- **Zip extraction drops the executable bit.** The backend sets it on the helper before the first run.
- **Where things go.** Decky exports the plugin's directories as environment variables before it imports `main.py`:
  settings in `~/homebrew/settings/romm-armada/`, data and backups in `~/homebrew/data/romm-armada/`, logs in
  `~/homebrew/logs/romm-armada/`. `paths.py` reads them, so nothing in the backend needs the `decky` module and the
  tests run without it (`ROMM_ARMADA_HOME` moves everything under one folder).
- **The settings file holds the RomM token.** Never paste it, and never read it while debugging a device; the log and
  `scripts/smoke_install.py` show everything that is needed.

## ARMSX2 and PCSX2

- **Data folder:** `~/.config/ARMSX2` (or `$XDG_CONFIG_HOME/ARMSX2`); PCSX2 uses `~/.config/PCSX2`. Its settings are in
  `inis/PCSX2.ini`.
- **Process:** on Armada, ARMSX2 is an AppImage. Its process shows up as `AppRun`, and its executable is `armsx2-qt`, so
  the watcher matches on the process name and on the executable name.
- **Which game:** an emulator started by a launcher has the disc on its command line. One started from its own menu does
  not, so the watcher looks at which installed game's disc image the process holds open (`/proc/<pid>/fd`). See
  [experiments.md](experiments.md), entry 6.
- **Memory cards:** a *directory* at the card's path is a folder card, a file is a file card. ARMSX2 has no setting for
  the kind. `[MemoryCards] Slot1_Filename` names the card in slot 1 (`Mcd001.ps2`).
- **BIOS folder:** `[Folders] Bios` in `PCSX2.ini`, relative to the data folder. The default is `<data>/bios`; the
  Armada's ARMSX2 points it at `~/Downloads`. A real BIOS has a ROM directory with a `RESET` entry on a 16-byte
  boundary in the first 512 KiB, and is 4 to 8 MiB.
- **argosy-sigil layout:** `pcsx2_standalone`. sigil sees a folder card only when there are files under
  `memcards/<card>/`, so a blank folder card needs a formatted `_pcsx2_superblock`.

## The panel in Steam's Quick Access Menu

The panel is React against `@decky/ui` and lives only in the Quick Access Menu. The lessons that cost time:

1. **A button that turns disabled, or is removed, while it has the controller focus loses it**, and the menu stops
   answering the pad. A touch scroll can then close the menu. So no button uses `disabled={busy}`. Re-entrancy is
   guarded instead: `useSingleFlight` (`src/components/Common.tsx`) makes a second press while the work runs do nothing.
2. **Keep rows keyed and stable.** A list that grows keeps the pressed row mounted under the same React key, so it is the
   same element and keeps the focus. The games list turns the pressed "Show more" row into a marker such as
   "Games 31–60" and adds the new games below it, so Down continues into them.
3. **Reopen where the reader was.** Coming back from a game's page reopens the list as long as it was (up to 300 games).

The rule for any new button: never `disabled={busy}`, never unmount the button that was just pressed.

### How it is checked without Steam

`test/ui/` renders the real panel in jsdom with stand-ins for `@decky/api` and `@decky/ui` and a scripted backend
(`bash scripts/test-ui.sh`). The focus rule is asserted by **element identity**: the test keeps the DOM element that was
pressed and checks that it is the same element after the next page arrives. This cannot reproduce Steam's own focus
handling, so the Show more fix is reasoned from how Steam treats focus and confirmed only in this harness. The panel
also type-checks against `@decky/ui` 4.11.

## Looking at a device safely

- Read the plugin's log and Decky's journal (`plugin_loader`); do not start games or touch settings files.
- `python3 scripts/smoke_install.py <zip>` unpacks a zip the way Decky does (no permission bits) and loads the plugin
  with a stand-in for Decky's module in a throwaway home. With `--real-home` and an installed copy it prints the BIOS
  and card state on the device.
- `scripts/send-to-armada.sh` copies the newest zip to the handheld's `~/Downloads` over SSH and compares checksums.
  It needs `ARMADA_HOST` (and optionally `ARMADA_KEY`).
