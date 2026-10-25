# Changelog

## 0.3.0

Saves now work the way Argosy and Freegosy do on the user's RomM (5.0.0), and a sync takes the latest save from anywhere.

- **Saves go to RomM as a zip of the game's save folders** (`BASLUS-21269BULLY000/…`, a few hundred KB) instead of an
  8 MB memory card image, when the server does not keep sigil units. RomM 5.5 and newer say so in their heartbeat
  (`SAVE_SYNC.SNAPSHOTS`) and still get the card image, as Argosy does. *Settings → Saves → Save format on RomM*:
  Automatic, Save folders or Card image. A server that changes format gets the same save once more in the new one.
- **Tagged `armadaos`**: the slot (default; existing installs move from the old `autosave` default) and the emulator tag
  of everything this device uploads, as Freegosy's are `freegosy`.
- **Pulls the latest save from any slot and any type.** A sync used to look only at its own slot, so Freegosy's saves
  (and Argosy's) were never seen; it now brings down the newest save RomM has for the game that this device has not
  taken, from whichever slot. It reads Freegosy's bundles, a zip of the game's folders, a whole folder card in a zip, a
  memory card image (also inside a zip, also a whole shared card, from which only the game's saves are taken) and sigil's
  per-game card. An older save than one already taken, a save state filed as a save, and a save that cannot be read are
  skipped. Where the last save came from is shown on the game screen.
- A restore of save folders replaces the game's own folders whole, takes nothing else off the card, writes only after
  checking the archive's paths, and puts the old folders back if it fails part way.
- About shows the RomM version the server reported and how saves go to it.

## 0.2.1

Found by reading the log of 0.2.0 on a real Armada.

- A game started from ARMSX2's own menu is now recognised by the disc image the emulator keeps open. The log used to
  say "unknown game" for it, which also meant no play time reached RomM. A game counts when it is the only one held
  open on two looks in a row, so the game list scan (which opens discs for a moment) cannot fool it. When the game was
  found this way, every installed game's save is still checked on exit (this one first), because the menu can switch
  games without closing the emulator.
- BIOS: the Armada's ARMSX2 is set to use `~/Downloads` as its BIOS folder (`Bios = ../../Downloads`). The screen now
  shows that folder without the `..`, lists only BIOS files (the other files are counted, not listed), and a file
  counts as a BIOS image when it has the ROM directory the emulator looks for, not by its size alone, so a folder like
  Downloads cannot hide the "no BIOS" warning.
- The log says what each save sync did and why.

## 0.2.0

- **BIOS files from RomM, when you ask.** The home screen says when the emulator's BIOS folder has none and offers
  *Get the BIOS from RomM* (also under Settings → Emulator → BIOS files). The screen lists the firmware RomM keeps for
  PlayStation 2 next to what the folder holds; *Install N missing files* or one file at a time. Nothing is placed
  unasked. A file that is already there with the same content is left alone (also when you renamed it); a different
  file with the same name is kept, and replacing it asks first and copies the old one to the plugin's backups.
  Every download is checked against RomM's size, MD5, SHA-1 and CRC32 before it gets its final name.
- The game screen shows what RomM keeps for a game's save: the card image (8.3 MB), which game saves are inside, and
  how much data they are. RomM always holds sigil's per-game memory card image; the card on the handheld stays a
  folder card.
- Pairing and sign-in ask for the `firmware.read` permission. A sign-in made with 0.1.0 does not have it: pair again to
  use the BIOS download (the screen says so).
- Fixed: the warning about a token that lacks permissions was set and thrown away with the connect screen, so it was
  never seen. The home screen now shows it, with what stops working.

## 0.1.0

First version.

- Connect to RomM with a typed **pairing code** (8 characters, 60 seconds, once), by approving a code shown on the
  handheld, with a pasted token, or with a password that is used once to make a token and never kept.
- Browse the RomM library by platform, search, and see what is installed; works from a cache when RomM is out of reach.
- Download games into one dedicated folder (default `~/ROMs`, or an SD card), with ES-DE folder names, resume,
  CRC32 checking, multi-file games, a free-space guard, a queue, speed limit, and recovery after a restart.
- Save sync for PlayStation 2 (ARMSX2 and PCSX2) through argosy-sigil in RomM's `autosave` slot: upload after the
  emulator closes, pull newer saves while idle, after waking and after installing a game, memory card backups,
  conflict choices, play time reporting.
- PS2 saves live on **folder memory cards**; a missing card is created, an empty file card is replaced, a file card
  with saves is never touched.
- Games sent to the device from RomM's web page ("install on device") are downloaded.
- `savebridge`, the helper around argosy-sigil, ships as static aarch64 and x86-64 builds.
