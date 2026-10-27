# Experiments

How the plugin got its shape. Each entry says what was asked, what was done, what came out and what changed in the
code. Versions refer to [CHANGELOG.md](../CHANGELOG.md).

Setting: a Retroid Nova running Armada OS with ARMSX2, the author's RomM server (5.0.0), and a PC with podman for
everything that can be tested without the handheld.

**The brief:** an Arm64-first Decky plugin that downloads games from RomM into a dedicated location and syncs saves
through [argosy-sigil](https://github.com/rommapp/argosy-sigil), starting with ARMSX2 and PCSX2, with real settings, a
pairing-code connect, and PCSX2 *folder* memory cards for the saves.

## 1. Can a C library run inside a Decky plugin on an Arm64 handheld?

**Question.** sigil is C. A Python binding would have to be loaded into Decky's own Python.

**Observation.** On Armada OS, Decky's bundled Python is an x86-64 program running under FEX. `platform.machine()`
says `x86_64` on an Arm64 device, and a native library of either architecture is awkward to load into it.

**Decision.** Run sigil in its own process. `savebridge` is a small C program that plays the part sigil expects of its
caller (sigil never touches the file system: the caller lists the save root, opens members, writes and removes files).
It takes paths on the command line, prints one JSON document on stdout, and moves blobs as files. It is built as a
**static** executable for aarch64 and for x86-64 in a Debian 12 container, so it needs no particular glibc.

**Result.**

- A static aarch64 executable starts natively from the x86-64 Python under FEX. On the handheld, Settings → About and
  the log report the aarch64 build. The x86-64 build is the fallback for a PC or a Steam Deck.
- Run under qemu on the PC, the aarch64 build gives the same units and hashes as the x86-64 build.
- `native/build.sh` pins sigil and its submodules to exact commits, and the two executables come out byte-for-byte the
  same on every build.
- Decky's zip extraction drops the executable bit, so the backend sets it before use. The plugin has no `root` flag:
  Decky then hands the plugin's files to the normal user, so downloads and saves belong to the user and the backend is
  allowed to chmod its helper.

## 2. What does sigil put on RomM for a PS2 save?

**Measured.** The unit sigil builds is a memory card image of **8,650,752 bytes** (8.25 MiB). It is the same size
whether the card on the device is a file or a folder, and it holds only the one game's save folder: in the test card,
one entry of 6 blocks, about 6 KB of data. Compressed it is about 38 KB.

**Consequence.** Every upload is 8.25 MB for a few KB of save. The unit can be restored onto any card layout, taking
only that game's saves, which is why sigil uses it. Versions 0.1.0 to 0.2.x shipped exactly this, in the `autosave` slot,
and the game screen explained what was inside (0.2.0).

## 3. Does a real RomM accept it?

A stand-in server covers the logic, but only a real server says whether the requests are accepted. The harness
(`test/test_real_romm.py`) starts RomM and MariaDB in podman, first 5.4.1, then 5.0.0 because that is the version the
author runs. What the real runs caught that the stand-in could not:

- RomM treats two devices with the same hostname and platform as **one device**. Two handhelds both called `armada`
  would merge, and the second would never be offered the first one's saves. Each install now registers under
  `<hostname>-<install id>`, which is also stable across re-pairing.
- RomM 5.0.0 starts library scans only over its websocket (`POST /api/tasks/scan` answers 405), so the harness runs the
  scan inside the container.
- A default download path of `~/ROMs` was not being expanded.
- The warning about a token that lacks permissions was created and thrown away together with the connect screen, so it
  was never seen. The home screen shows it now, with what stops working.
- sigil's `content_hash` is the one RomM computes for the same bytes.

## 4. Connecting with a pairing code

Typing a long token on a handheld is miserable, so the first target was RomM's pairing code. What is known, from RomM's
source and from the real servers:

- The code is 8 characters from `ABCDEFGHJKMNPQRSTUVWXYZ23456789` (no I, L, O, 0 or 1), lives **60 seconds**, works
  **once**, and the server allows 5 tries a minute.
- On a gamepad keyboard, 60 seconds is tight, so the code is formatted as typed (`ABCD-EFGH`) and checked locally
  before the request is sent. A typo then costs nothing from the 5-a-minute budget.
- Two fallbacks: the handheld shows a code to approve in RomM's web page (RomM's device flow), or a pasted token. A
  password is accepted once to mint a token and never kept.
- Against real servers: the code works once, a second use is explained, the rate limit is explained, and a token
  without the needed permissions is explained.

## 5. First field test: 0.1.0 on the handheld

It worked: ARMSX2 started, the sync settled on a folder card, and a save made in a game reached RomM. It also showed two
things.

1. **The upload was 8.25 MB for a tiny save.** That is sigil's unit (entry 2), but it was not what the author's other
   clients put on the same server. See entry 7.
2. **The log said "unknown game" at every exit.** ARMSX2 had been started from its own menu, not with a disc on the
   command line, so the plugin did not know which game was played. The fallback (push every changed save) worked, but
   no play time was ever queued. See entry 6.

## 6. Which game is the emulator playing?

A launcher such as ES-DE passes the disc image on the emulator's command line. ARMSX2's own menu does not. On Armada
the emulator is an AppImage whose process shows up as `AppRun` (its executable is `armsx2-qt`).

**Idea.** Look at which installed game's disc image the process holds open (`/proc/<pid>/fd`).

**Care taken.** Scanning the game list in the emulator opens discs for a moment, so a game counts only when it is the
**single** game held open on two looks in a row, two seconds apart. Because the emulator's menu can switch games
without closing it, a game found this way still has every installed game's save checked on exit (this one first), and
only the saves that changed are uploaded.

**Result.** On the handheld, the log shows the game found by its open disc image (Bully, God Hand). Shipped in 0.2.1.

## 7. What do the other clients put on RomM?

**Observation.** Saves made by the author's other RomM clients, on the same server, were small zips, not 8 MB units.

**Reading their sources.**

- [Freegosy](https://github.com/abduznik/Freegosy) uploads `<Game>.zip`: a `freegosy_sync.txt` plus the game's save
  folders at the root of the zip, in the slot `freegosy`. When it syncs it takes the **newest save of the game from any
  slot**.
- [Argosy](https://github.com/rommapp/argosy-launcher) uses sigil's per-game units only when the server's heartbeat has
  `SAVE_SYNC.SNAPSHOTS` (RomM 5.5 and newer, according to its source). Older servers get a zip of the game's folders.
- The author's server is 5.0.0, and its heartbeat has no `SAVE_SYNC` key.

**Correction.** The 8 MB unit had looked like the standard because it is what sigil produces. Seeing what the clients
beside it actually keep changed the default.

**Changed in 0.3.0.** Saves go up as a zip of the game's save folders unless the server says it keeps units. They are
tagged `armadaos` (slot and emulator), as Freegosy's are `freegosy`. A sync pulls from any slot. Details of every shape
are in [save-formats.md](save-formats.md).

## 8. The hash that makes the same save the same save

For a zip, RomM's `content_hash` is not the hash of the file's bytes. It is the MD5 of the lines `name:md5(entry)` over
the zip's entries, sorted by name and joined with newlines (`hash_zip_contents` in RomM's source). The same save zipped
by two different tools therefore has the same hash.

The plugin computes the same value, sends it with the upload, and uses it to tell "unchanged" from "changed". The real
server test checks that the hash RomM stores for an uploaded save equals the one computed locally, on 5.0.0 and on
5.4.1. RomM does not unpack a zip entry larger than 512 MiB to hash it and falls back to the bytes; the plugin does the
same.

## 9. A sync takes the newest save from any slot

**How RomM's negotiation behaves** (see [romm-notes.md](romm-notes.md)): on 5.0.0 the request carries only the device
and its saves, and the answer contains download operations for the unlisted slot heads of **all** of the user's games,
so the plugin picks out the game it is syncing. Saves that sit in no slot never appear in it, so the plugin lists a
game's saves itself and treats the newest slot-less one as a candidate.

**The rules.**

- Candidates from every slot compete. The newest one that this device has not taken yet wins (a per-game `seen`
  watermark records what was taken).
- A save older than one already taken is never restored over it. A save state filed as a save and a save that could not
  be read earlier are skipped.
- First contact with a game where RomM holds a *different* save, or a save that changed on both sides, is a conflict
  that is asked about, never decided on a guess.

**A surprise.** After restoring a save from another slot, the first upload to this device's own slot got `409 Slot has
a newer save since your last sync`. RomM keeps a sync record per saved version, so a device that has not seen the
slot's head is refused. When the slot's head is older than what the device already took, the upload is now repeated
with `overwrite=true`.

**Verified** on real RomM 5.0.0 and 5.4.1: a Freegosy-shaped save in its own slot is pulled as the latest, and the next
save from the handheld goes to the `armadaos` slot.

## 10. The failure that was correct

The first test of 0.3.0 on the handheld: Bully's save went up as the right zip (about 128 KB, one folder, the hash on
the server equal to the local one). Pulling the save of Tekken 4 reported a failure.

Looking inside the zips on RomM explained it: they were an older client's upload of a **whole PC memory card** (the
folders of other games, such as Tekken Tag Tournament and God of War, and an empty second card) and contained no Tekken
4 save at all. There was nothing to restore, and saying so was the right behaviour.

**Changed in 0.3.1.** The message names what a whole card holds ("it has BASLUS-20001TekkenTT, BASCUS-97399GodOfWar
…") instead of a bare "nothing for this game". The author chose not to import those old saves.

## 11. Taking only this game's part out of a whole card

**Question.** Can a `.ps2` that is already on RomM be read, and only the game's own part taken, so that folder saves
can be the default while the old `.ps2` files stay where they are?

**Answer.** Yes. sigil restores one game out of a whole shared card. For zips, the plugin's planner keeps only the
folders whose names belong to the game and counts the rest as ignored (a card, a Freegosy bundle with save states, and
a whole folder card inside a zip all work the same way). The `.ps2` on RomM is never modified. From then on the device
uploads folders. A device whose card is a file keeps using the card image, since a file card cannot give folders.

**Changed in 0.3.2.** `saves.format` defaults to `folders`. The earlier default was `auto`, which followed the server.

## 12. ARMSX2 on folder cards by default

**Observation.** ARMSX2 has no setting for the kind of a memory card. A **directory** at the card's path
(`memcards/Mcd001.ps2`) is a folder card, a file is a file card. On the handheld, slot 1 pointed at `Mcd001.ps2`, and
slot 2 was an unused 8 MB file.

**Detail.** sigil sees a folder card only when files exist under `memcards/<card>/`, so a blank folder card needs a
formatted `_pcsx2_superblock`. The helper has a command for that (`init-folder-card`).

**Rules (0.3.2).** The card is prepared when the plugin starts, after pairing, after the handheld wakes and after the
emulator closes: never while the emulator runs, never over a card that holds saves. A missing card is created. An empty
file card is moved aside (`.empty-<time>.bak`) and replaced. This way the card is a folder card before the first game
is ever played.

## 13. BIOS files from RomM's firmware list

A PS2 emulator will not start a game without a BIOS, and RomM keeps firmware per platform. The plugin installs BIOS
files **only on request**, and:

- lists RomM's firmware for the PlayStation 2 beside what the folder holds, with a state for each file (missing,
  installed, present under another name, different);
- never replaces a different file that has the same name without asking, and copies the old one to its backups first;
- checks size, MD5, SHA-1 and CRC32 against RomM's values before a download gets its final name.

**A lesson from the handheld.** The Armada's ARMSX2 uses `~/Downloads` as its BIOS folder, shared with everything else
downloaded there. Judging by size alone would call any 4 to 8 MB download a BIOS and hide the "no BIOS" warning. A file
counts as a BIOS only when it is 4 to 8 MiB **and** its ROM directory has a `RESET` entry on a 16-byte boundary in the
first 512 KiB. The folder screen then lists only BIOS files and counts the rest. Shipped in 0.2.1.

## 14. "Show more" killed the controller

**Report.** Pressing "Show more" at the end of a games list left the controller unresponsive, and a touch scroll then
closed the Quick Access Menu.

**Most likely cause** (Steam's focus handling cannot be run on a PC, so this is reasoned, not observed): buttons set
`disabled={busy}` while their work ran. Steam takes the controller focus away from a button that becomes disabled or
leaves the screen, and with nothing focused the menu stops answering the pad.

**0.3.1.** No button disables itself while its action runs. A `useSingleFlight` guard makes a second press while the
work runs do nothing, and the list keeps one stable row at its end. That was "better but not perfect": the focus stayed
on a button that moved to the end of the list.

**0.3.2.** The row that was pressed stays exactly where it is (same React key, so the same element) and becomes a
marker such as "Games 31–60". The new games load below it and a new "Show more" closes the list, so pressing Down goes
on into the games just loaded.

**Checked** in the jsdom harness by asserting that the DOM element is the same before and after the page arrives. Not
checked in Steam itself.

## 15. RetroArch: what sigil covers and what it does not

**Question.** Armada's store has more emulators than ARMSX2. RetroArch plays most of the 8-bit and 16-bit systems, so it was
the next one to try.

**What sigil does there.** RetroArch is a frontend: the save depends on the core that runs the game, and sigil has a
"layout" per core. The default libretro layout fits every core that keeps its battery save under the game's own name:
`<game>.srm`, plus `<game>.rtc` when the cart has a clock. A trial with the helper showed the limit:

- `locate` (which files are the game's save) works with only the game's file name, on any system.
- `collect` and `restore` (build and write the unit) need sigil to identify the game first, and sigil can do that only for
  some systems. A Game Boy ROM worked. A Game Boy Advance, NES or Mega Drive file stopped with `unknown_platform`, and
  passing a made-up identity did not get round it. sigil's own platform notes agree: those systems are "located", not
  "synced".

**Decision.** The default row is two files, so the plugin follows it directly (`libretro.py`) instead of through sigil's
sync: the `.srm` goes up as it is, and with a clock the `.srm` and `.rtc` go up as a flat zip named `<game>.srm.zip`, which
is what Argosy uploads for the same row. A test runs the helper's `locate --hash` on the same files and checks that its
artifact name and RomM hash equal the plugin's. Restoring writes the files under *this* device's game name, whatever the
uploader called theirs, and puts the old files back if the write fails half way.

**Where RetroArch keeps its saves** is the part that varies, so it is read, not assumed: `retroarch.cfg` gives
`savefile_directory`, `savefiles_in_content_dir` and the two sorting switches. With sorting by core, the core's folder is
not known until a game has been played once, so a game with no save yet says so instead of guessing a folder. With
sorting by game folder the name is the folder the game is in. A save that exists is found wherever it is.

**Verified** against a stand-in server with two devices (upload, second device, newer save, conflicts, another client's
slot, a save that is not a battery save) and against real RomM (hashes, stored names, and a second device restoring the
`.srm` and `.rtc`). Not verified: a real RetroArch on a handheld. The handheld was not reachable when this was written, so
where Armada OS's RetroArch keeps its saves is still to be looked at.

## Dead ends and corrections

- A Python binding for sigil was rejected for the architecture reason in entry 1.
- The card image was first the format that was sent, and only a look at the other clients changed that (entry 7). A
  version of this plugin with an opt-in "folders only" upload had been offered and set aside before that look.
- A rollback test showed that when a restore failed half way, only the folders already swapped were put back. The
  restore now remembers everything it moved and placed, and undoes all of it.
- A size check rejected old Freegosy bundles because they carried save states of tens of MB. Only the entries that would
  actually be written count against the memory card's size now.
- The first "Show more" fix kept the focus on a button that moved (entry 14).

## Open questions

- A real RomM 5.5 or newer, to run the card-image path that servers keeping units use. It is tested against a stand-in
  for the heartbeat flag only.
- The Show more behaviour and the BIOS download on the handheld itself, and gamepad navigation of every screen.
- Other emulators and systems. PlayStation 2 (ARMSX2, PCSX2) and the RetroArch systems listed in the README are wired for
  saves; downloads work for every platform in the library. PlayStation 1, DS, N64 and the others need per-core layouts.
- ES-DE starting a game that was just downloaded.
