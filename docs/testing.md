# Testing

Five layers, from cheap to expensive. Each one exists because the cheaper ones could not catch something.

| Layer | Command | What it covers |
|---|---|---|
| Backend | `python3 -m pytest test` | 220 tests with an in-process RomM stand-in and the real `savebridge` helper |
| Panel | `bash scripts/test-ui.sh` | 24 scenarios, the real panel rendered in jsdom against a scripted backend |
| Real RomM | `ROMM_REAL=1 python3 -m pytest test/test_real_romm.py -s` | 10 checks against a real server and database in podman |
| Arm64 parity | part of the backend run, when `qemu-aarch64-static` is installed | the aarch64 helper under qemu gives the same units and hashes as x86-64 |
| Install | `python3 scripts/smoke_install.py <zip>` | the zip loads the way Decky loads it and the first screen's data comes back |

## Backend (`test/`)

- `mock_romm.py` is a stand-in for the endpoints the plugin uses, written to follow RomM's own code (saves, sync
  negotiation, device auth, client tokens) closely enough that the sync logic meets the answers a real server gives.
- `fixtures.py` builds a minimal PS2 disc image (so sigil can read a game id), save zips of every shape, and a fake
  ARMSX2 data folder.
- Test modules: `test_core` (settings, helper, pairing), `test_downloads` (resume, corrupted files, free space, queue),
  `test_saves` (sync end to end with the real helper), `test_folders` (folder zips, every archive shape, cross-slot
  pulls, rollbacks), `test_bios`, `test_service` (the Decky RPC layer under a stand-in for Decky's module, the watcher,
  remote installs), `test_send_script`.
- Everything runs in an isolated home folder; nothing touches the real one.

## Panel (`test/ui/`)

`harness.tsx` mounts React in jsdom and drives it by clicking, typing and settling. The stubs for `@decky/api` and
`@decky/ui` are in `test/ui/stubs/`. Scenarios: typed pairing code, device approval, browsing, downloading, conflicts,
BIOS files, settings, disconnecting, and the "Show more" focus behaviour (asserted by DOM element identity). Needs
`pnpm install` once.

## Real RomM (`test/test_real_romm.py`)

It starts MariaDB and RomM in podman on a private network, adds a small library with a generated PS2 disc, runs a scan,
and exercises the plugin against it. The images must already be pulled; nothing is downloaded by the test.

```sh
ROMM_REAL=1 python3 -m pytest test/test_real_romm.py -s
ROMM_REAL=1 ROMM_IMAGE=docker.io/rommapp/romm:5.0.0 python3 -m pytest test/test_real_romm.py -s
```

The checks: pairing with a real code, a token without permissions being explained, device approval, library and a
download verified against RomM's CRC32, a zip of save folders whose `content_hash` equals the local one and a second
device restoring it, the card image format, conflicts, play time, the latest save pulled from any slot, and firmware
upload, listing, download and install into a BIOS folder. A failed start removes its containers so the port is free for
the next run.

On 5.0.0 the scan is run inside the container, because that version starts scans only over its websocket.

## What this does not cover

Steam's own focus handling and rendering, the BIOS download on a handheld, a real RomM 5.5 or newer, and emulators other
than ARMSX2 and PCSX2. See the list in the [README](../README.md#verified-and-not-yet-verified).

## Reproducible builds

`native/build.sh` builds the two helper executables in a Debian 12 container from the revision pinned in
`native/SIGIL_PIN` (its submodules are checked against pins too) and gives the same bytes every time. `scripts/package.sh`
makes the zip with fixed timestamps and ordering, runs both helpers (aarch64 under qemu when available), and refuses a
zip that contains caches or test files.
