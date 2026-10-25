# RomM Armada

A [Decky Loader](https://decky.xyz) plugin that connects a handheld to your [RomM](https://romm.app) server:

- **Download games** from your RomM library into one dedicated folder (default `~/ROMs`, or an SD card), with
  resume, checksums, a space check and a queue.
- **Sync saves** with RomM through [argosy-sigil](https://github.com/rommapp/argosy-sigil), the same library Argosy
  and RomM's web player use, so a save made here continues on your phone and the other way round.
- **Arm64 first.** Built for Arm64 handhelds running [Armada OS](https://armadaos.dev) (Retroid, AYN, AYANEO…); the
  helper that does the save work ships as a static aarch64 build, with an x86-64 build as the fallback for a Steam
  Deck or for Decky running under FEX.

The first system is **PlayStation 2**, with **ARMSX2** (the Arm64 PCSX2 fork Armada installs as
`~/Applications/ARMSX2.AppImage`) or **PCSX2**. Downloading works for every platform in your library.

> Status: tested on a PC against a real RomM 5.4.1 server (in podman), with the Arm64 helper run under qemu, and tried
> on a Retroid handheld running Armada OS (its owner reports: ARMSX2 launched, the sync settled on a folder card and a
> save made in a game was pushed to RomM correctly). See [Verified and not yet verified](#verified-and-not-yet-verified).

## Install

1. Decky menu → Settings → Developer → *Install Plugin from Zip* → pick `RomM-Armada-0.3.2.zip`.
2. Open the plugin from Decky's Quick Access menu.

## Connect

On the first screen enter your server address, then either:

- **Pair with a code** *(recommended)*: in RomM's web page open your API tokens, make a token with all
  permissions (the plugin uses firmware read for BIOS files too) and choose **Pair**. RomM shows an 8-character code. Type it on the handheld within **60 seconds**
  (it works once). The code is formatted for you (`ABCD-EFGH`) and checked before it is sent.
- **Show a code on the handheld** and approve it in RomM's web page while signed in there.
- Or paste a token, or sign in with a password (used once to make a token; never stored).

The device registers itself with RomM, so its saves are tracked per device ("conflict" detection, play time).

## Saves and the memory card

Sync works per game:

1. **After you close the emulator**, the plugin looks at that game's saves and uploads them if they changed. It knows
   the game from the emulator's command line or, when you started it from ARMSX2's own menu, from the disc image the
   emulator keeps open; that game's play time is reported to RomM too.
2. **While no emulator runs** (at start, every N minutes, after waking from sleep) it brings down saves that are
   newer on RomM, and when you install a game it fetches the save made elsewhere. Pressing *Sync* does the same on demand.
3. **Nothing is replaced on a guess.** If this device never synced a game and RomM already has a *different* save,
   or both sides changed, the plugin asks (*Keep RomM's / Keep this device's / Keep both*), or follows the policy
   you choose in Settings. A card is backed up before anything is written to it.

### Tagged `armadaos`, pulled from anywhere

Every save this device uploads is tagged **`armadaos`**, as its slot and as its emulator on RomM (Freegosy's are
`freegosy`). The slot is a setting; the tag is only for naming. **Pulling ignores the tag:** a sync looks at every save
RomM has for the game, in any slot, whoever made it (Freegosy, Argosy, another handheld), and brings down the newest one
this device has not taken yet. Playing here then uploads to the `armadaos` slot and leaves the others as they were.
A save older than one already taken is never restored over it, and a save state filed as a save is skipped.

### What a save looks like on RomM

- **Save folders (zip)**, the default: a zip with the game's save folders at its root (`BASLUS-21269BULLY000/…`): a few
  hundred KB, never the other games on the card. This is what Argosy and Freegosy upload to a RomM before 5.5. RomM hashes
  a zip by its entries, so the same save is the same save however it was zipped.
- **Card image**: sigil's per-game 8.3 MB `.ps2`, which Argosy uses on RomM 5.5 and newer (the server says so in its
  heartbeat). Used when the setting asks for it, or when the card here is a file (a file card cannot give folders).
- *Settings → Saves → Save format on RomM*: Save folders (default), Automatic (follows the server, as Argosy), or Card
  image. A change gets the same save uploaded once more in the new format.

A `.ps2` already on RomM is read, not skipped: the plugin looks inside the card (whole shared cards too), takes only
this game's save folders out of it and puts them on the folder card, and the other games' saves on it are ignored. The
`.ps2` on RomM is left as it is; from then on this device uploads folders. What it can **restore** is wider: both of the above, Freegosy's bundles (its `freegosy_sync.txt` is ignored), a whole folder
card in a zip (only this game's folders are taken), a memory card image inside a zip, and a whole shared `.ps2` file
card (only this game's saves are taken). A restore replaces the game's own save folders and nothing else on the card,
and puts everything back if it fails half way. A save that cannot be read is skipped once, with the reason, and not
asked for again. The game screen shows what RomM holds and which slot the last save came from.

### Folder memory cards

PS2 saves are kept on **PCSX2 folder memory cards** (a folder per save inside `memcards/Mcd001.ps2/`), the same
mode Argosy uses on Android. A restore writes only that game's save folders; other games' saves are never rewritten.
Each upload is a new version in the slot; *Keep only recent versions on RomM* with *Versions on RomM* (default 10) bounds
how many stay.

- ARMSX2 has no setting for the kind of a card: it takes a directory at the card's path for a folder card. So the plugin
  makes slot 1 a folder card **before a game is ever played**: when it starts, after pairing, after the handheld wakes
  and after the emulator closes (not only when a game is installed), as long as the emulator is installed or has run.
- No card yet → the plugin creates a formatted folder card.
- A file card with no saves → replaced by a folder card (the old file is kept beside it as `.bak`).
- A file card **with** saves is never touched: convert it in the emulator (Settings → Memory Cards → Convert), or set
  *Card type* to *Use what the emulator has* (saves then travel as card images).

## BIOS files

A PS2 emulator cannot start a game without a BIOS. When the emulator's BIOS folder has none, the home screen says so
and offers **Get the BIOS from RomM** (also Settings → Emulator → BIOS files). That screen lists the firmware RomM keeps
for the PlayStation 2 platform (in RomM: the platform, then *Firmware*) beside what the folder holds.

- Nothing is placed unasked: *Install N missing files*, or tap one file.
- A file that is already there with the same content is left alone, also if you renamed it.
- A different file with the same name is kept. Replacing it asks first and copies the old one to the plugin's backups.
- Each download is checked against RomM's size, MD5, SHA-1 and CRC32 before it gets its final name.
- The folder is `[Folders] Bios` in the emulator's `PCSX2.ini`, `~/.config/ARMSX2/bios` by default (the Armada's
  ARMSX2 uses `~/Downloads`). ARMSX2 and PCSX2 pick up a BIOS in that folder by themselves. A shared folder is fine:
  only BIOS files are listed (a file counts as one when it is 4 to 8 MiB and has the ROM directory the emulator
  looks for), the rest is counted and left alone.
- It needs the *firmware read* permission. A sign-in made with plugin 0.1.0 lacks it: the screen says so, and pairing
  again fixes it.

## Where things live

| What | Where |
|---|---|
| Games | `Settings → Games and downloads → Download location` (default `~/ROMs/<system>/`; ES-DE names like `ps2`, `psx`, `gc`) |
| ARMSX2 data | `~/.config/ARMSX2` (or `$XDG_CONFIG_HOME/ARMSX2`); PCSX2: `~/.config/PCSX2` |
| BIOS files | `<data folder>/bios` unless the emulator's `PCSX2.ini` says otherwise |
| Backups of replaced BIOS files | `~/homebrew/data/romm-armada/backups/bios/` |
| Plugin settings, per-game sync records | `~/homebrew/settings/romm-armada/` |
| Backups of memory cards | `~/homebrew/data/romm-armada/backups/game-<id>/` |
| Logs | `~/homebrew/logs/romm-armada/` |

The plugin runs as your normal user (not root), so files it creates belong to you.

## Settings

Everything is under the plugin's **Settings**: download location (home, SD card, folder browser, or typed path),
folder naming, extras, checksum mode, simultaneous downloads, speed limit, free space to keep, unpacking `.zip`,
resuming after restart, accepting games sent from RomM's web page; saves on/off, slot name, when to upload and pull,
conflict policy, memory card type, backups, versions kept on RomM, play-time reporting, notifications; which PS2
emulator and where its data folder is; device name and certificate checking.

## Build and test

```sh
./native/build.sh          # bin/savebridge-aarch64 and bin/savebridge-x86_64 (podman, Debian 12; byte-for-byte reproducible)
pnpm install && pnpm run build
./scripts/package.sh       # RomM-Armada-<version>.zip

python3 -m pytest test                    # backend: RomM stand-in, the real helper (aarch64 too, under qemu-aarch64-static)
bash scripts/test-ui.sh                   # the panel rendered in jsdom against a scripted backend
ROMM_REAL=1 python3 -m pytest test/test_real_romm.py -s    # a real RomM + MariaDB in podman (images must be pulled)
```

## Verified and not yet verified

**Verified on a PC**

- Against **a real RomM, 5.4.1 and 5.0.0** (the version the plugin's first user runs; `test/test_real_romm.py`, 10 checks
  each): pairing with a real code (works once, the 5-a-minute limit is explained, a token without the right permissions
  is explained), approving a code shown on the device, listing the library, a real download checked against RomM's
  CRC32, a save uploaded as a zip of the game's save folders in the `armadaos` slot whose RomM `content_hash` equals the
  one the plugin computed, a second device restoring it onto a folder card, the card image format against the real
  server, "first contact" and "keep both" conflict handling, play time reaching RomM, a Freegosy-shaped save in its own
  slot pulled as the latest and the handheld's next save going to the `armadaos` slot, and firmware uploaded, listed,
  downloaded and checked into the BIOS folder (with the server's refusal of a sign-in that lacks the firmware
  permission). These runs also caught that RomM merges devices with the same hostname, so each install now registers
  under its own, and that 5.0.0 starts scans only over its websocket (the test harness runs the scan inside the container).
- The save helper on **x86-64 and aarch64 (qemu)**: identical units and hashes; builds reproduce exactly.
- 220 backend tests (downloads with resume and corruption, save sync in both formats and across slots, every
  archive shape, BIOS install, the watcher, remote installs, the Decky RPC layer under a stand-in for Decky's module) and
  24 rendered panel scenarios (typed pairing code, device
  approval, browsing, downloading, conflicts, BIOS files, settings, disconnecting). The panel also type-checks against
  `@decky/ui` 4.11.
- **On a handheld** (reported by its owner, 0.1.0 on a Retroid with Armada OS): ARMSX2 launched, the sync settled on
  a folder card, and a save made in a game was pushed to RomM correctly.

**Not verified**

- The BIOS download on a handheld (0.2.x), Steam's real rendering of the new screens, and gamepad navigation of them.
- ARMSX2's own memory cards beyond that first play session (the tests build cards with sigil), and ES-DE starting a
  downloaded game.
- Which helper build gets picked on the handheld: Settings → About shows it (`aarch64` is expected).

## License

MIT for the plugin. The save helper contains [argosy-sigil](https://github.com/rommapp/argosy-sigil) (MPL-2.0,
unmodified) and its bundled libraries; see [THIRD_PARTY.md](THIRD_PARTY.md).
