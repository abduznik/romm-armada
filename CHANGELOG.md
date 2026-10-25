# Changelog

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
