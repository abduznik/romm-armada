# RomM API notes

What the plugin relies on, and how it differs between RomM versions. The sources are RomM's public code
(`endpoints/saves.py`, `endpoints/sync`, `endpoints/device`, `endpoints/roms`, `handler/filesystem/assets_handler.py`)
and real servers: 5.0.0 and 5.4.1, run in podman by `test/test_real_romm.py`. The server's version is in the heartbeat
(`GET /api/heartbeat`, `SYSTEM.VERSION`).

## Signing in

| Way | Calls |
|---|---|
| Pairing code | `POST /api/client-tokens/exchange` with `{"code": …}` returns the token (`raw_token`) |
| Device approval (code shown on the handheld) | `POST /api/auth/device/init`, then `POST /api/auth/device/token` until approved. A `400` with `authorization_pending`, `slow_down`, `access_denied` or `expired_token` is the answer while waiting |
| Password, used once | `POST /api/token` (OAuth password grant), then `POST /api/client-tokens` to make a token; the password is never kept |
| A pasted token | Used as given |

The pairing code is 8 characters from `ABCDEFGHJKMNPQRSTUVWXYZ23456789`, valid 60 seconds, usable once, 5 tries a minute.

The token is a *client token* with scopes. The plugin asks for `me.read`, `roms.read`, `platforms.read`, `assets.read`,
`assets.write`, `devices.read`, `devices.write`, `roms.user.read`, `roms.user.write` and `firmware.read`. Nothing that
edits the library. A token made by plugin 0.1.0 lacks `firmware.read`, and the BIOS screen says so.

## Devices

`POST /api/devices` with `allow_existing`. **RomM merges devices that have the same hostname and platform.** Two
handhelds that are both called `armada` would become one device in RomM, so the plugin registers under
`<hostname>-<install id>`. The id is stable, so pairing again finds the same device.

## Library

`GET /api/platforms`, `GET /api/roms` (platform ids, search, paging, order), `GET /api/roms/{id}` and the file content at
`/api/roms/{id}/files/content/{name}`. Game checksums come from the file list, and downloads are checked against CRC32.

## Firmware (BIOS)

| Call | Needs |
|---|---|
| `GET /api/firmware?platform_id=` | `firmware.read` |
| `GET /api/firmware/{id}/content/{name}` | `firmware.read` |
| `POST /api/firmware?platform_id=` (multipart, `files`) | `firmware.write` |

A token without `firmware.read` is refused with a clear message, which the BIOS screen shows.

## Saves

- `GET /api/saves?rom_id=&device_id=&slot=` lists saves. `GET /api/saves/{id}/content` downloads one, and
  `POST /api/saves/{id}/downloaded` confirms it with a hash.
- `POST /api/saves` uploads: multipart field `saveFile`, and the query `rom_id`, `emulator`, `slot`, `device_id`,
  `content_hash`, `session_id`, `overwrite`, `autocleanup`, `autocleanup_limit`.
- **A slot refuses an upload (`409`, "Slot has a newer save since your last sync") when its newest save is one this
  device has not seen.** `overwrite=true` replaces it. The plugin sets it only when the slot's head is older than a
  save this device already took from another slot.
- A save in **no slot** is never offered by the negotiation (below).
- The `content_hash` of a zip is computed from its entries: see [save-formats.md](save-formats.md).
- RomM puts a timestamp before the last extension of an upload's name: `Game.srm` is stored as `Game [<time>].srm`, and the
  zip `Game.srm.zip` as `Game.srm [<time>].zip`.

## Save states

RomM's states are a separate API from saves, with no slots, devices or negotiation. `POST /api/states?rom_id=&emulator=`
(multipart `stateFile`, optional `screenshotFile`) stores the file under the emulator's folder and **replaces the row of the
same file name**; `GET /api/states?rom_id=` lists them (`id`, `file_name`, `emulator`, `file_size_bytes`, `updated_at`; no content
hash); `GET /api/states/{id}/content` downloads one. The same calls exist on 5.0.0 and 5.4.1. With no hash and no device record,
the plugin keeps its own record of what it last synced (a hash of the local file and RomM's `updated_at`) and compares RomM's
timestamp with the one it recorded. Scope: `assets.read` and `assets.write`, which the plugin already asks for.

## The sync negotiation

`POST /api/sync/negotiate`, then `POST /api/sync/sessions/{id}/complete` with the counts.

| | 5.0.0 | 5.4.1 |
|---|---|---|
| Request | `device_id` and `saves` only | also `rom_ids`, `emulators`, `restore_unlisted` |
| Answer | Download operations for the unlisted slot heads of **all** the user's games | Narrowed by `rom_ids` and `emulators` |

The plugin sends `rom_ids` and no emulator filter (a save is pulled whoever made it) and keeps only the operations for
the game being synced, so it behaves the same on both.

RomM keeps a sync record per saved version. A device that has not seen the newest version of a slot is told to
`download` even when its own save changed, judged by timestamps alone, so the plugin decides with hashes and turns that
case into a conflict instead of replacing the save.

## Which servers keep sigil units

Argosy's source decides by the heartbeat: `SAVE_SYNC.SNAPSHOTS` present means the server keeps sigil's per-game units
(RomM 5.5 and newer, per that source). The 5.0.0 and 5.4.1 servers tested here do not have the key. The plugin follows
the same flag when `saves.format` is `auto`. This path is tested against a stand-in, not a real 5.5 server.

## Library scans

On 5.0.0 a scan starts only over the websocket: `POST /api/tasks/scan` answers 405. This only matters for tests, which
run the scan inside the container.

## Other endpoints used

- `POST /api/play-sessions` reports play time (start, end, duration, save slot).
- `POST /api/devices/{id}/installs/claim` and `PUT /api/devices/{id}/installs/{request}` take part in "install on
  device" from RomM's web page, when the setting allows it.
