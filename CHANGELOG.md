# Changelog

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
