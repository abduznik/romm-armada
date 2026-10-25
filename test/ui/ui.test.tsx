import { buttons, choose, cleanup, click, findButton, input, mount, settle, slide, text, toggle, type, act } from "./harness";
import assert from "node:assert/strict";
import { afterEach, beforeEach, describe, test } from "node:test";
import React from "react";
import { Root } from "../../src/Root";
import { alerts, downloads, startEvents } from "../../src/store";
import { backend, fire, listenerCount, reset, toaster } from "./stubs/api";
import { clearModals, modals } from "./stubs/ui";

const settingsDoc = () => ({
  ok: true,
  server: { url: "http://romm.local:8080", verify_tls: true },
  auth: { device_id: "d1", device_name: "Armada", username: "admin", method: "pairing", signed_in: true },
  library: { root: "/home/armada/ROMs", folder_style: "esde", platform_dirs: {}, include_extras: true, verify: "crc", max_concurrent: 1,
    speed_limit_kbs: 0, min_free_mb: 512, extract_zip: false, resume_on_start: true, remote_installs: false },
  saves: { enabled: true, slot: "autosave", push_on_exit: true, pull_on_startup: true, pull_when_idle: true, pull_interval_min: 30,
    conflict: "ask", card_type: "folder", backups: true, backup_keep: 5, autocleanup: true, autocleanup_limit: 10, playtime: true, notify: true },
  emulators: { ps2: { emulator: "auto", data_dir: "", executable: "" } },
  advanced: { request_timeout: 30, debug: false },
});

const overview = (over: any = {}) => ({
  ok: true, version: "0.1.0", signed_in: true,
  server: { url: "http://romm.local:8080", username: "admin", method: "pairing", verify_tls: true },
  device: { id: "d1", name: "Armada" },
  library: { root: "/home/armada/ROMs", free: 50e9, total: 100e9, installed: 1, installed_size: 4e9 },
  downloads: { active: 0, total: 0, failed: 0 },
  emulator: { name: "ARMSX2", id: "armsx2", data_dir: "/home/armada/.config/ARMSX2", data_dir_exists: true, found_by: "found",
    executable: "/home/armada/Applications/ARMSX2.AppImage", running: false,
    card: { slot1: { name: "Mcd001.ps2", kind: "folder", enabled: true }, wanted: "folder", custom_dir: false },
    bios: { dir: "/home/armada/.config/ARMSX2/bios", has_bios: true, count: 1 } },
  saves: { enabled: true, conflicts: [], card_type: "folder" },
  helper: { ok: true, arch: "aarch64", helper: "0.1.0", sigil: "5d4ddb41cc22" },
  ...over,
});

const conflict = {
  rom_id: 101, name: "Test Game (USA)", reason: "first_contact", detail: "", at: "2026-10-25T10:00:00Z",
  local: { updated_at: "2026-10-25T09:00:00Z", hash: "aa", has_saves: true },
  server: { save_id: 7, updated_at: "2026-10-24T09:00:00Z", hash: "bb", device: "Pixel" },
};

const detail = (over: any = {}) => ({
  ok: true, id: 101, name: "Test Game (USA)", fs_name: "Test Game (USA).iso", platform: "PlayStation 2", platform_slug: "ps2",
  summary: "A game.", size: 4_000_000_000, regions: ["USA"], languages: [], genres: ["Racing"], companies: [],
  files: [{ name: "Test Game (USA).iso", size: 4_000_000_000, category: "game" }], cover: "", installed: false, local: null,
  download: null, offline: "", saves: { rom_id: 101, supported: true }, saves_supported: true, ...over,
});

const biosItem = (over: any = {}) => ({
  id: 1, name: "scph70012.bin", size: 4194304, verified: true, md5: "aa", held_as: "scph70012.bin", state: "missing", ...over,
});

const biosStatus = (over: any = {}) => ({
  ok: true, emulator: { name: "ARMSX2", id: "armsx2", running: false },
  dir: "/home/armada/.config/ARMSX2/bios", dir_exists: false, local: [], has_bios: false,
  remote: {
    ok: true, platform: { id: 1, name: "PlayStation 2" }, reason: "", message: "",
    items: [biosItem(), biosItem({ id: 2, name: "scph39001.bin", verified: false })],
  },
  ...over,
});

const noBios = () => overview({ emulator: { ...overview().emulator, bios: { dir: "/home/armada/.config/ARMSX2/bios", has_bios: false, count: 0 } } });

function backendWith(over: Record<string, (...a: any[]) => any> = {}) {
  let settings: any = settingsDoc();
  backend.handlers = {
    overview: () => overview(),
    downloads: () => ({ ok: true, items: [] }),
    conflicts: () => ({ ok: true, items: [] }),
    settings_get: () => settings,
    settings_set: (patch: any) => {
      const merge = (a: any, b: any): any => {
        for (const k of Object.keys(b)) a[k] = typeof b[k] === "object" && !Array.isArray(b[k]) ? merge(a[k] ?? {}, b[k]) : b[k];
        return a;
      };
      settings = merge(structuredClone(settings), patch);
      return { ok: true, changed: patch, settings };
    },
    platforms: () => ({ ok: true, stale: false, platforms: [{ id: 1, name: "PlayStation 2", slug: "ps2", fs_slug: "ps2", rom_count: 2, folder: "ps2", installed: 1, saves: true }] }),
    roms: () => ({ ok: true, total: 2, offset: 0, stale: false, items: [
      { id: 101, name: "Test Game (USA)", fs_name: "Test Game (USA).iso", size: 4e9, platform_id: 1, platform_slug: "ps2", regions: ["USA"], cover: "", installed: false, download: null },
      { id: 102, name: "Other Game", fs_name: "Other.iso", size: 1e9, platform_id: 1, platform_slug: "ps2", regions: [], cover: "", installed: true, download: null }] }),
    rom: () => detail(),
    ...over,
  };
  return () => settings;
}

describe("the panel", () => {
  afterEach(async () => {
    await cleanup();
  });

  beforeEach(() => {
    reset();
    clearModals();
    downloads.set({});
    alerts.set({ authRequired: false, conflicts: {}, emulatorRunning: false, missingScopes: [] });
  });

  test("connecting with a pairing code: formatted as typed, checked, sent, then the home screen", async () => {
    let signedIn = false;
    backendWith({
      overview: () => overview({ signed_in: signedIn }),
      settings_get: () => ({ ...settingsDoc(), server: { url: "", verify_tls: true } }),
      connect_code: () => {
        signedIn = true;
        return { ok: true, username: "admin", device_id: "d1", missing_scopes: [] };
      },
    });
    const h = await mount(<Root />);
    await settle(3);
    assert.match(text(h), /Pair with a code/);
    assert.match(text(h), /60 seconds/);
    assert.equal(findButton(h, "Pair this device").disabled, true, "nothing to pair with yet");

    await type(h, "Server address", "romm.local:8080");
    await type(h, "Pairing code", "abc");
    assert.equal(input(h, "Pairing code").value, "ABC");
    assert.equal(findButton(h, "Pair this device").disabled, true, "three characters are not a code");
    await type(h, "Pairing code", "abcd efgh");
    assert.equal(input(h, "Pairing code").value, "ABCD-EFGH");
    assert.equal(findButton(h, "Pair this device").disabled, false);

    await click(h, "Pair this device");
    const call = backend.calls.find((c) => c.method === "connect_code")!;
    assert.deepEqual(call.args, ["romm.local:8080", "ABCD-EFGH", true]);
    assert.match(text(h), /Browse the library/, "the home screen follows a successful pairing");
    await h.unmount();
  });

  test("a code the server refuses shows why, and the code field is cleared", async () => {
    backendWith({
      overview: () => overview({ signed_in: false }),
      connect_code: () => ({ ok: false, error: "code_expired", message: "That code is not valid any more. Codes last 60 seconds and work once" }),
    });
    const h = await mount(<Root />);
    await settle(3);
    await type(h, "Server address", "http://romm.local:8080");
    await type(h, "Pairing code", "ABCDEFGH");
    await click(h, "Pair this device");
    assert.match(text(h), /Codes last 60 seconds/);
    assert.equal(input(h, "Pairing code").value, "");
    await h.unmount();
  });

  test("showing a code on the handheld and approving it elsewhere", async () => {
    let signedIn = false;
    let release!: () => void;
    const gate = new Promise<void>((r) => (release = r));
    backendWith({
      overview: () => overview({ signed_in: signedIn }),
      connect_device_start: () => ({ ok: true, active: true, user_code: "WXYZ-2345", verify_url: "http://romm.local:8080/pair/device?user_code=WXYZ2345", seconds_left: 600 }),
      connect_device_poll: async () => {
        await gate;
        signedIn = true;
        return { ok: true, status: "approved", username: "admin", missing_scopes: [] };
      },
    });
    const h = await mount(<Root />);
    await settle(3);
    await type(h, "Server address", "http://romm.local:8080");
    await click(h, "Show a code on this device");
    assert.match(text(h), /WXYZ-2345/);
    assert.match(text(h), /pair\/device\?user_code=WXYZ2345/);
    release();
    await settle(4);
    assert.match(text(h), /Browse the library/);
    await h.unmount();
  });

  test("a token with missing permissions is called out", async () => {
    let signedIn = false;
    backendWith({
      overview: () => overview({ signed_in: signedIn }),
      connect_token: () => {
        signedIn = true;
        return { ok: true, username: "admin", missing_scopes: ["assets.write"] };
      },
    });
    const h = await mount(<Root />);
    await settle(3);
    await type(h, "Server address", "http://romm.local:8080");
    await click(h, "Token, password, certificate");
    await type(h, "Client token", "rmm_abc");
    await click(h, "Use this token");
    assert.deepEqual(backend.calls.find((c) => c.method === "connect_token")!.args, ["http://romm.local:8080", "rmm_abc", true]);
    assert.match(text(h), /Browse the library/, "the token works, so the user is let in");
    assert.match(text(h), /lacks assets\.write \(uploading saves\)/, "and told what will not work");
    assert.match(text(h), /allow all of them/);
    await click(h, "Pair again");
    assert.match(text(h), /Pair with a code/);
    await h.unmount();
  });

  test("browse, open a game, download it, watch the progress", async () => {
    backendWith({ install: () => ({ ok: true, rom_id: 101, state: "queued" }) });
    const stop = startEvents();
    const h = await mount(<Root />);
    await settle(3);
    assert.match(text(h), /armada|Armada/);
    await click(h, "Browse the library");
    assert.match(text(h), /PlayStation 2/);
    await click(h, "PlayStation 2");
    assert.match(text(h), /Test Game \(USA\)/);
    assert.match(text(h), /● Other Game/, "installed games carry a dot");
    await click(h, "Test Game (USA)");
    assert.match(text(h), /Download · 3\.7 GB/);
    await click(h, /^Download/);
    assert.deepEqual(backend.calls.find((c) => c.method === "install")!.args, [101]);

    await act(async () => {
      fire("download_progress", { rom_id: 101, name: "Test Game (USA)", platform: "PS2", platform_slug: "ps2", state: "running", error: "", error_kind: "",
        total: 4_000_000_000, done: 2_000_000_000, speed: 25_000_000, eta: 80, files: 1, created: "", finished: "" });
    });
    await settle(1);
    assert.match(text(h), /1\.9 GB of 3\.7 GB/);
    assert.match(text(h), /1 min left/);
    assert.ok(buttons(h).some((b) => /Pause/.test(b.textContent ?? "")));
    await h.unmount();
    stop();
    assert.equal(listenerCount(), 0, "events are unhooked when the plugin is removed");
  });

  test("a failed download says why and offers a retry", async () => {
    backendWith({ download_resume: () => ({ ok: true }), rom: () => detail({ download: { rom_id: 101, state: "error", error: "The drive is full", error_kind: "disk", total: 4e9, done: 1e9, speed: 0, eta: 0, files: 1, name: "x", platform: "", platform_slug: "ps2", created: "", finished: "" } }) });
    const h = await mount(<Root />);
    await settle(3);
    await click(h, "Browse the library");
    await click(h, "PlayStation 2");
    await click(h, "Test Game (USA)");
    assert.match(text(h), /The drive is full/);
    await click(h, "Try again");
    assert.ok(backend.calls.some((c) => c.method === "download_resume"));
    await h.unmount();
  });

  test("saves that need a decision are announced and can be settled", async () => {
    backendWith({
      overview: () => overview({ saves: { enabled: true, conflicts: [conflict], card_type: "folder" } }),
      conflicts: () => ({ ok: true, items: [conflict] }),
      sync_game: (id: number, choice: string) => ({ ok: true, status: "downloaded", message: "Restored RomM's save onto this device", rom_id: id, name: "Test Game (USA)" }),
    });
    const h = await mount(<Root />);
    await settle(3);
    assert.match(text(h), /1 save needs a decision/);
    await click(h, "1 save needs a decision");
    assert.match(text(h), /never synced this game/);
    assert.match(text(h), /from Pixel/);
    await click(h, "Keep RomM's save");
    assert.equal(modals.length, 1, "it asks before replacing anything");
    await act(async () => (modals[0].props as any).onOK());
    await settle(3);
    assert.deepEqual(backend.calls.find((c) => c.method === "sync_game")!.args, [101, "server"]);
    assert.match(text(h), /Restored RomM's save/);
    await h.unmount();
  });

  test("settings write only what changed, as a nested patch, and show what the backend refuses", async () => {
    const get = backendWith();
    const h = await mount(<Root />);
    await settle(3);
    await click(h, "Settings");
    await click(h, "Saves and memory card");
    await toggle(h, "Upload when the emulator closes");
    assert.deepEqual(backend.calls.filter((c) => c.method === "settings_set").pop()!.args, [{ saves: { push_on_exit: false } }]);
    assert.equal(get().saves.push_on_exit, false);
    assert.equal(input(h, "Upload when the emulator closes").checked, false);

    await choose(h, "Card type", "auto");
    assert.deepEqual(backend.calls.filter((c) => c.method === "settings_set").pop()!.args, [{ saves: { card_type: "auto" } }]);

    await slide(h, "Check every", 45);
    assert.deepEqual(backend.calls.filter((c) => c.method === "settings_set").pop()!.args, [{ saves: { pull_interval_min: 45 } }]);

    backend.handlers.settings_set = () => ({ ok: false, errors: { "saves.slot": "must not be empty" }, message: "" });
    await type(h, "Slot name", "");
    await click(h, "Save slot name");
    assert.match(text(h), /saves\.slot: must not be empty/);
    await h.unmount();
  });

  test("the memory card can be set up from the saves settings, and the folder-card policy is spelled out", async () => {
    backendWith({
      overview: () => overview({ emulator: { ...overview().emulator, card: { slot1: { name: "Mcd001.ps2", kind: "file", enabled: true }, wanted: "folder", custom_dir: false } } }),
      prepare_cards: () => ({ ok: true, message: "Memory card ready: created a folder card" }),
    });
    const h = await mount(<Root />);
    await settle(3);
    assert.match(text(h), /is a file card\. Folder cards are used for sync/);
    await click(h, "Settings");
    await click(h, "Saves and memory card");
    assert.match(text(h), /Folder card \(recommended\)/);
    assert.match(text(h), /Slot 1.*Mcd001\.ps2 · file card/);
    await click(h, "Set up the memory card");
    assert.match(text(h), /created a folder card/);
    await h.unmount();
  });

  test("the download location can come from an SD card or the folder browser, and a bad one is refused", async () => {
    backendWith({
      volumes: () => ({ ok: true, items: [{ path: "/var/home/armada/ROMs", name: "Home folder", total: 1e11, free: 5e10 }, { path: "/run/media/mmcblk0p1", name: "mmcblk0p1", total: 2.56e11, free: 2e11 }] }),
      check_location: (p: string) => (p.startsWith("/root") ? { ok: false, message: "/root is not writable by this user" } : { ok: true, path: p, exists: true, free: 2e11, total: 2.5e11, message: "" }),
    });
    const h = await mount(<Root />);
    await settle(3);
    await click(h, "Settings");
    await click(h, "Games and downloads");
    await choose(h, "Choose a drive", "/run/media/mmcblk0p1/ROMs");
    assert.deepEqual(backend.calls.filter((c) => c.method === "settings_set").pop()!.args, [{ library: { root: "/run/media/mmcblk0p1/ROMs" } }]);
    assert.match(text(h), /Games now go to \/run\/media\/mmcblk0p1\/ROMs/);

    await click(h, "Choose another folder");
    assert.deepEqual(backend.calls.filter((c) => c.method === "check_location").pop()!.args, ["/run/media/mmcblk0p1/ROMs"]);
    await type(h, "Folder path", "/root/ROMs");
    await click(h, "Save folder path");
    await h.unmount();
  });

  test("disconnecting asks first, then returns to the connect screen", async () => {
    let signedIn = true;
    backendWith({ overview: () => overview({ signed_in: signedIn }), disconnect: () => ((signedIn = false), { ok: true }) });
    const h = await mount(<Root />);
    await settle(3);
    await click(h, "Settings");
    await click(h, "Connection");
    await click(h, "Disconnect");
    assert.equal(modals.length, 1);
    await act(async () => (modals[0].props as any).onOK());
    await settle(3);
    assert.match(text(h), /Connect to RomM/);
    await h.unmount();
  });

  test("events become toasts only when they deserve one", async () => {
    backendWith();
    const stop = startEvents();
    const base = { rom_id: 101, name: "Test Game (USA)", message: "", notify: true };
    fire("sync_result", { ...base, status: "uploaded", trigger: "exit" });
    fire("sync_result", { ...base, status: "uploaded", trigger: "manual" });
    fire("sync_result", { ...base, status: "up_to_date", trigger: "auto" });
    fire("sync_result", { ...base, status: "downloaded", trigger: "auto", notify: false });
    assert.deepEqual(toaster.toasts.map((t) => t.title), ["Save uploaded"]);
    fire("sync_result", { ...base, status: "conflict", trigger: "auto", conflict });
    assert.equal(toaster.toasts.at(-1).title, "Choose which save to keep");
    assert.ok(alerts.get().conflicts[101]);
    fire("sync_result", { ...base, status: "uploaded", trigger: "manual" });
    assert.equal(alerts.get().conflicts[101], undefined, "a conflict that was settled is forgotten");
    fire("auth_required");
    fire("auth_required");
    assert.equal(toaster.toasts.filter((t) => t.title === "RomM sign-in needed").length, 1, "asked once, not per request");
    stop();
  });

  test("without a BIOS the home screen says so, and the BIOS screen installs what RomM has", async () => {
    let state = biosStatus();
    backendWith({
      overview: () => (state.has_bios ? overview() : noBios()),
      bios_status: () => state,
      bios_install: (ids: number[] | null, overwrite: boolean) => {
        state = biosStatus({
          dir_exists: true, has_bios: true,
          local: [{ name: "scph70012.bin", size: 4194304, kind: "bios" }, { name: "scph39001.bin", size: 4194304, kind: "bios" }],
          remote: { ...state.remote, items: state.remote.items.map((i: any) => ({ ...i, state: "installed" })) },
        });
        return { ok: true, message: "Installed 2 files in /home/armada/.config/ARMSX2/bios.", report: { installed: ["scph70012.bin", "scph39001.bin"], skipped: [], conflicts: [], busy: [], failed: [] }, status: state };
      },
    });
    const h = await mount(<Root />);
    await settle(3);
    assert.match(text(h), /ARMSX2 has no BIOS yet, and cannot start a game without one/);
    await click(h, "Get the BIOS from RomM");
    assert.match(text(h), /\/home\/armada\/\.config\/ARMSX2\/bios/);
    assert.match(text(h), /The folder does not exist yet/);
    assert.match(text(h), /From RomM \(PlayStation 2\)/);
    assert.match(text(h), /scph70012\.bin/);
    assert.match(text(h), /4\.0 MB · verified by RomM · not in the folder/);
    assert.match(text(h), /scph39001\.bin/);
    await click(h, "Install 2 missing files");
    assert.deepEqual(backend.calls.find((c) => c.method === "bios_install")!.args, [null, false]);
    assert.match(text(h), /Installed 2 files in \/home\/armada\/\.config\/ARMSX2\/bios\./);
    assert.equal(findButton(h, "Everything from RomM is in the folder").disabled, true);
    assert.equal(findButton(h, "✓ scph70012.bin").disabled, true, "a file that is there cannot be fetched again");
    assert.doesNotMatch(text(h), /cannot start a game without a BIOS image/);
    await click(h, "Back");
    assert.doesNotMatch(text(h), /has no BIOS yet/, "the home screen follows");
    await h.unmount();
  });

  test("one file by itself, and a file that would replace the player's own asks first", async () => {
    const calls: any[] = [];
    backendWith({
      overview: () => noBios(),
      bios_status: () => biosStatus({
        dir_exists: true, local: [{ name: "scph39001.bin", size: 4194304, kind: "bios" }], has_bios: true,
        remote: {
          ok: true, platform: { id: 1, name: "PlayStation 2" }, reason: "", message: "",
          items: [biosItem(), biosItem({ id: 2, name: "scph39001.bin", state: "different" }), biosItem({ id: 3, name: "scph10000.bin", state: "present", held_as: "mine.bin" })],
        },
      }),
      bios_install: (ids: number[], overwrite: boolean) => (calls.push([ids, overwrite]), { ok: true, message: "Installed 1 file in /x.", report: { installed: ["x"], skipped: [], conflicts: [], busy: [], failed: [] } }),
    });
    const h = await mount(<Root />);
    await settle(3);
    await click(h, "Get the BIOS from RomM");
    assert.match(text(h), /in the folder under another name \(mine\.bin\)/);
    assert.match(text(h), /a different file has this name/);
    assert.equal(findButton(h, "scph10000.bin").disabled, true);
    await click(h, "Install 1 missing file");
    assert.deepEqual(calls.pop(), [null, false]);
    await click(h, /^scph70012\.bin/);
    assert.deepEqual(calls.pop(), [[1], false]);
    await click(h, /^scph39001\.bin/);
    assert.equal(modals.length, 1, "it asks before replacing a file the player put there");
    assert.equal(calls.length, 0);
    assert.match(String((modals[0].props as any).strDescription), /copied to the plugin's backups first/);
    await act(async () => (modals[0].props as any).onOK());
    await settle(3);
    assert.deepEqual(calls.pop(), [[2], true]);
    await h.unmount();
  });

  test("a sign-in without firmware access is explained and can be fixed by pairing again", async () => {
    backendWith({
      overview: () => noBios(),
      bios_status: () => biosStatus({
        remote: { ok: false, platform: null, items: [], reason: "forbidden", message: "This device's sign-in does not include firmware access (the permission firmware.read). To fix it, pair again and allow it when RomM asks." },
      }),
    });
    const h = await mount(<Root />);
    await settle(3);
    await click(h, "Get the BIOS from RomM");
    assert.match(text(h), /does not include firmware access/);
    assert.equal(buttons(h).some((b) => /Install/.test(b.textContent ?? "")), false, "nothing to install without access");
    await click(h, "Pair again");
    assert.match(text(h), /Pair with a code/);
    await h.unmount();
  });

  test("RomM with no firmware says where to add it, and a running emulator is mentioned", async () => {
    backendWith({
      overview: () => noBios(),
      bios_status: () => biosStatus({
        emulator: { name: "ARMSX2", id: "armsx2", running: true }, dir_exists: true,
        local: [{ name: "readme.txt", size: 20, kind: "other" }],
        remote: { ok: true, platform: { id: 1, name: "PlayStation 2" }, items: [], reason: "empty", message: "RomM has no firmware for PlayStation 2. Add the BIOS in RomM (open the platform, then Firmware), or put the file in the folder below yourself." },
      }),
    });
    const h = await mount(<Root />);
    await settle(3);
    await click(h, "Get the BIOS from RomM");
    assert.match(text(h), /RomM has no firmware for PlayStation 2/);
    assert.match(text(h), /readme\.txt/);
    assert.match(text(h), /not a BIOS image/);
    assert.match(text(h), /ARMSX2 is running\. Files can be added now, not replaced\./);
    assert.equal(buttons(h).some((b) => /Install/.test(b.textContent ?? "")), false);
    await h.unmount();
  });

  test("a failed install says which file and why, and keeps the list usable", async () => {
    backendWith({
      overview: () => noBios(),
      bios_status: () => biosStatus(),
      bios_install: () => ({
        ok: false, message: "Installed 1 file in /x. scph39001.bin: The connection ended before the file was complete.",
        report: { installed: ["scph70012.bin"], skipped: [], conflicts: [], busy: [], failed: [{ name: "scph39001.bin", error: "scph39001.bin: The connection ended before the file was complete", kind: "network" }] },
        status: biosStatus({ remote: { ...biosStatus().remote, items: [biosItem({ state: "installed" }), biosItem({ id: 2, name: "scph39001.bin", state: "missing" })] } }),
      }),
    });
    const h = await mount(<Root />);
    await settle(3);
    await click(h, "Get the BIOS from RomM");
    await click(h, "Install 2 missing files");
    assert.match(text(h), /scph39001\.bin: The connection ended before the file was complete/);
    assert.equal(findButton(h, "Install 1 missing file").disabled, false, "and the rest can be tried again");
    await h.unmount();
  });

  test("the BIOS files are also reachable from the emulator settings", async () => {
    backendWith({ bios_status: () => biosStatus({ has_bios: true, dir_exists: true, local: [{ name: "scph70012.bin", size: 4194304, kind: "bios" }] }) });
    const h = await mount(<Root />);
    await settle(3);
    assert.doesNotMatch(text(h), /has no BIOS yet/, "a BIOS is there, so the home screen stays quiet");
    await click(h, "Settings");
    await click(h, "Emulator");
    assert.match(text(h), /1 in \/home\/armada\/\.config\/ARMSX2\/bios/);
    await click(h, "BIOS files");
    assert.match(text(h), /scph70012\.bin/);
    await h.unmount();
  });

  test("the game screen says what RomM keeps for the save, so the whole card image is not a surprise", async () => {
    backendWith({
      rom: () => detail({
        installed: true, local: { main_file: "/home/armada/ROMs/ps2/Test Game (USA).iso", path: "/home/armada/ROMs/ps2", size: 4e9, installed_at: "2026-10-25T09:00:00Z" },
        saves: {
          rom_id: 101, supported: true, enabled: true, emulator: "ARMSX2", slots: [{ slot: 1, enabled: true, name: "Mcd001.ps2", kind: "folder" }],
          last: { at: new Date().toISOString(), action: "upload", unit: { size: 8650752, contents: ["BASLUS-20152"], bytes: 6144 } },
        },
      }),
    });
    const h = await mount(<Root />);
    await settle(3);
    await click(h, "Browse the library");
    await click(h, "PlayStation 2");
    await click(h, "Test Game (USA)");
    assert.match(text(h), /RomM keeps/);
    assert.match(text(h), /8\.\d MB card image: BASLUS-20152, 6\.0 KB of save data/);
    assert.match(text(h), /holds only this game's save\. The card on this device stays a folder card/);
    assert.match(text(h), /Memory card.*Mcd001\.ps2 \(folder\)/);
    await h.unmount();
  });

  test("when RomM stops accepting the device the home screen says so and offers to pair again", async () => {
    backendWith();
    const stop = startEvents();
    const h = await mount(<Root />);
    await settle(3);
    await act(async () => fire("auth_required"));
    await settle(1);
    assert.match(text(h), /no longer accepts this device/);
    await click(h, "Pair again");
    assert.match(text(h), /Pair with a code/);
    assert.match(text(h), /stopped accepting this device/);
    await h.unmount();
    stop();
  });
});
