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

> Status: tested on a PC against a real RomM 5.4.1 server (in podman), with the Arm64 helper run under qemu.
> **Not yet run on a handheld** (the Retroid was offline). See [Verified and not yet verified](#verified-and-not-yet-verified).

## Install

1. Decky menu → Settings → Developer → *Install Plugin from Zip* → pick `RomM-Armada-0.1.0.zip`.
2. Open the plugin from Decky's Quick Access menu.

## Connect

On the first screen enter your server address, then either:

- **Pair with a code** *(recommended)*: in RomM's web page open your API tokens, make a token with all
  permissions and choose **Pair**. RomM shows an 8-character code. Type it on the handheld within **60 seconds**
  (it works once). The code is formatted for you (`ABCD-EFGH`) and checked before it is sent.
- **Show a code on the handheld** and approve it in RomM's web page while signed in there.
- Or paste a token, or sign in with a password (used once to make a token; never stored).

The device registers itself with RomM, so its saves are tracked per device ("conflict" detection, play time).

## Saves and the memory card

Sync works per game, in RomM's `autosave` slot:

1. **After you close the emulator**, the plugin looks at that game's saves and uploads them if they changed.
2. **While no emulator runs** (at start, every N minutes, after waking from sleep) it brings down saves that are
   newer on RomM, and when you install a game it fetches the save the other devices made.
3. **Nothing is replaced on a guess.** If this device never synced a game and RomM already has a *different* save,
   or both sides changed, the plugin asks (*Keep RomM's / Keep this device's / Keep both*), or follows the policy
   you choose in Settings. A card is backed up before anything is written to it.

### Folder memory cards

PS2 saves are kept on **PCSX2 folder memory cards** (a folder per save inside `memcards/Mcd001.ps2/`), the same
mode Argosy uses on Android. A restore writes only that game's save folders; other games' saves and the card file
are never rewritten. The unit sent to RomM is the same either way (a per-game card), so a device using a file card
interoperates too.

- No card yet → the plugin creates a formatted folder card.
- A file card with no saves → replaced by a folder card (the old file is kept beside it as `.bak`).
- A file card **with** saves is never touched: convert it in the emulator (Settings → Memory Cards → Convert), or set
  *Card type* to *Use what the emulator has*.

## Where things live

| What | Where |
|---|---|
| Games | `Settings → Games and downloads → Download location` (default `~/ROMs/<system>/`; ES-DE names like `ps2`, `psx`, `gc`) |
| ARMSX2 data | `~/.config/ARMSX2` (or `$XDG_CONFIG_HOME/ARMSX2`); PCSX2: `~/.config/PCSX2` |
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

- Against **a real RomM 5.4.1** (`test/test_real_romm.py`): pairing with a real code (works once, the 5-a-minute limit
  is explained, a token without the right permissions is explained), approving a code shown on the device, listing
  the library, a real download checked against RomM's CRC32, a save uploaded to the `autosave` slot as
  `armsx2_refresh` whose RomM `content_hash` equals the MD5 sigil reported, a second device restoring it onto a
  folder card, "first contact" and "keep both" conflict handling, and play time reaching RomM. This run also caught
  that RomM merges devices with the same hostname, so each install now registers under its own.
- The save helper on **x86-64 and aarch64 (qemu)**: identical units and hashes; builds reproduce exactly.
- 111 backend tests (downloads with resume and corruption, save sync, the watcher, remote installs, the Decky RPC
  layer under a stand-in for Decky's module) and 13 rendered panel scenarios (typed pairing code, device approval,
  browsing, downloading, conflicts, settings, disconnecting). The panel also type-checks against `@decky/ui` 4.11.

**Not verified**

- Running inside Decky on an Armada handheld (where Decky itself runs under FEX), Steam's real rendering of the
  panel, and gamepad navigation of it.
- ARMSX2's own memory cards from a real play session (the tests build cards with sigil), and ES-DE starting a
  downloaded game.
- Which helper build gets picked on the handheld: Settings → About shows it (`aarch64` is expected).

## License

MIT for the plugin. The save helper contains [argosy-sigil](https://github.com/rommapp/argosy-sigil) (MPL-2.0,
unmodified) and its bundled libraries; see [THIRD_PARTY.md](THIRD_PARTY.md).
