# Changelog

## 0.4.0

- **RetroArch saves.** Games for Game Boy, Game Boy Color, Game Boy Advance, NES, Super Nintendo, Mega Drive, Master System
  and Game Gear sync their battery saves with RomM. RetroArch keeps one `<game>.srm` (and a `<game>.rtc` for a cart with a
  clock) per game in its save folder, and that file goes to RomM the way Argosy sends it: the `.srm` as it is, or a zip of
  the two when there is a clock. RetroArch's folder is read from its `retroarch.cfg` (`savefile_directory`, saves next to
  the game, sorting by core or by game folder); a save that already exists is found wherever it is. When RetroArch sorts
  saves by core and a game has none yet, the game says to play it once first.
- **Dolphin: GameCube saves.** sigil reads the disc's game id and moves that game's `.gci` files (from Dolphin's GCI folder,
  or out of a raw memory card) as one unit, the way Argosy does. Dolphin's User folder is found in the Flatpak's data
  folder (also `~/.local/share/dolphin-emu` and `~/.dolphin-emu`), and `SlotA` / `MemoryCardSize` are read from
  `Dolphin.ini`. Wii saves and slot B are not covered yet.
- The same rules as the PS2 saves apply to both: the newest save from any slot is pulled, nothing is replaced on a guess,
  the old files are backed up first (and put back if writing fails), and the slot and tag are `armadaos`.
- *Settings → Emulator* has a RetroArch and a Dolphin section (what was found, and fields to point at another folder). The
  home screen counts the games for each, and the game screen says what RomM keeps.
- A PS2 emulator running no longer holds back a Game Boy or GameCube sync, and the other way round; only the emulator that
  plays the game does. The watcher recognises RetroArch and Dolphin.
- Fixed: a game that RomM keeps as a `.zip` failed every download with "did not match RomM's checksum". RomM lists the CRC32
  of one file inside a zipped game (the first), not of the zip, and some zips hold a dozen versions of the game. A zip that
  reads back whole and holds a file with that CRC is accepted. Anything else is still refused, and the log says which
  checksums it saw.
- **Save states sync too.** RomM keeps states apart from saves (its *states*: one row per game and file name, no slots, no
  history, filed under the emulator's own folder), and the plugin now uses that. ARMSX2 and PCSX2 (`SLUS-21269 (28703748).01.p2s`
  in the `sstates` folder from `PCSX2.ini`), Dolphin (`GZLE01.s01` in `StateSaves`) and RetroArch (`<game>.state`,
  `.state1`…, `.state.auto`, in its `savestate_directory`, sorted the way its config says) are covered. Per file: a state
  that is new or changed here is uploaded; one that is only on RomM is downloaded (once: a state you deleted here does not
  come back); one RomM has newer than the last sync replaces the local file after a copy of it is kept aside; one that
  changed on both sides is left alone and the sync says so. Nothing is ever deleted, on either side. The emulator's other
  files (`.backup`, thumbnails, the emulator's resume state) are not touched.
  *Settings → Saves → Sync save states* turns it off.

## 0.3.2

- "Show more": the row you pressed stays where it was and the next games load below it, so the selection is where you
  were reading and Down goes on into the new games (0.3.1 kept the focus on a button that moved to the end of the list).
  The row becomes a marker ("Games 31–60") and a new "Show more" closes the list.
- **Save folders are the default format** (0.3.0 saved "Automatic", which followed the server). A `.ps2` that is already
  on RomM is still read: only this game's saves are taken out of it, whole shared cards included, and it is left on RomM.
  A device whose memory card is a file still uses the card image, because a file card cannot give folders.
- **Memory card slot 1 is made a folder card before a game is played**: when the plugin starts, after pairing, after
  the handheld wakes and after the emulator closes, not only when a game is installed. An empty file card from the
  emulator's first start is replaced (and kept aside), a card with saves is never touched.

## 0.3.1

- Fixed: after pressing "Show more" in the games list the Quick Access Menu stopped answering the controller, and
  scrolling by touch then closed it. The button turned itself off while it loaded, and Steam takes the controller's focus
  away from a button that turns off (or goes away). A long list now keeps one row at its end for good ("Show more (60 of
  70)", then "All 70 games are shown"), and no button disables itself while its action runs any more: Sync saves now,
  Download, Sync this save now, Pair this device, the BIOS buttons and the save decisions press again and do nothing
  until the work is done.
- Coming back from a game's page reopens the games list as long as it was, instead of the first 30.
- A save that is a whole memory card with none of this game's saves on it now says whose saves it holds ("it has
  BASLUS-20001TekkenTT, BASCUS-97399GodOfWar …"). Found when a Tekken 4 entry on RomM turned out to be Freegosy's old upload of a
  whole PC card with other games' saves and none of Tekken 4's.

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
