// The shapes the Python side sends. Kept by hand next to py_modules/rommarmada/service.py.

export type Failure = { ok: false; error: string; message: string; errors?: Record<string, string> };
export type Success<T = object> = { ok: true } & T;
export type Result<T = object> = Success<T> | Failure;

export type Settings = {
  server: { url: string; verify_tls: boolean };
  auth: { device_id: string; device_name: string; username: string; method: string; signed_in: boolean };
  library: {
    root: string; folder_style: "esde" | "romm"; platform_dirs: Record<string, string>; include_extras: boolean;
    verify: "crc" | "size" | "none"; max_concurrent: number; speed_limit_kbs: number; min_free_mb: number;
    extract_zip: boolean; resume_on_start: boolean; remote_installs: boolean;
  };
  saves: {
    enabled: boolean; slot: string; push_on_exit: boolean; pull_on_startup: boolean; pull_when_idle: boolean;
    pull_interval_min: number; conflict: "ask" | "server" | "device" | "newest"; card_type: "folder" | "auto";
    format: "auto" | "folders" | "card";
    backups: boolean; backup_keep: number; autocleanup: boolean; autocleanup_limit: number; playtime: boolean;
    notify: boolean;
  };
  states: { enabled: boolean };
  emulators: {
    ps2: { emulator: "auto" | "armsx2" | "pcsx2"; data_dir: string; executable: string };
    retroarch: { data_dir: string; save_dir: string; executable: string };
    dolphin: { data_dir: string; executable: string };
  };
  advanced: { request_timeout: number; debug: boolean };
};

export type CardInfo = {
  slot1: { name: string; kind: "folder" | "file" | "missing" | "other"; enabled: boolean };
  wanted: "folder" | "auto";
  custom_dir: boolean;
};

export type EmulatorInfo = {
  name: string; id: string; data_dir: string; data_dir_exists: boolean; found_by: string; executable: string;
  running: boolean; card: CardInfo; bios: { dir: string; has_bios: boolean; count: number };
};

/** RetroArch: whether it is there, where its saves go, and how many installed games use it. */
export type RetroArchInfo = {
  name: string; data_dir: string; data_dir_exists: boolean; found_by: string; executable: string; present: boolean; games: number;
  save_dir: string; next_to_game: boolean; sort_by_core: boolean; sort_by_content: boolean; running: boolean;
};

/** Dolphin: whether it is there, where its User folder is, and how many installed GameCube games use it. */
export type DolphinInfo = {
  name: string; data_dir: string; data_dir_exists: boolean; found_by: string; executable: string; present: boolean; games: number;
  raw_card: boolean; running: boolean;
};

export type Conflict = {
  rom_id: number; name: string; reason: "first_contact" | "both_changed" | "server_moved" | string; detail: string;
  local: { updated_at: string | null; hash: string; has_saves: boolean };
  server: { save_id: number | null; updated_at: string | null; hash: string | null; device: string; slot?: string };
  at: string;
};

export type Overview = {
  version: string;
  signed_in: boolean;
  server: { url: string; username: string; method: string; verify_tls: boolean };
  device: { id: string; name: string };
  library: { root: string; free: number; total: number; installed: number; installed_size: number };
  downloads: { active: number; total: number; failed: number };
  emulator: EmulatorInfo;
  retroarch?: RetroArchInfo;
  dolphin?: DolphinInfo;
  saves: { enabled: boolean; conflicts: Conflict[]; card_type: string };
  helper: { ok: boolean; arch?: string; helper?: string; sigil?: string; error?: string };
};

export type Platform = {
  id: number; name: string; slug: string; fs_slug: string; rom_count: number; folder: string; installed: number;
  saves: boolean;
};

export type DownloadState = "queued" | "running" | "paused" | "verifying" | "done" | "error" | "cancelled";

export type DownloadJob = {
  rom_id: number; name: string; platform: string; platform_slug: string; state: DownloadState; error: string;
  error_kind: string; total: number; done: number; speed: number; eta: number; files: number; created: string;
  finished: string;
};

export type RomSummary = {
  id: number; name: string; fs_name: string; size: number; platform_id: number; platform_slug: string;
  regions: string[]; cover: string; installed: boolean; download: DownloadJob | null;
};

export type SyncResult = {
  rom_id: number; name: string; message: string; reason?: string; conflict?: Conflict; trigger?: string; notify?: boolean;
  status: "uploaded" | "downloaded" | "up_to_date" | "no_saves" | "conflict" | "busy" | "blocked" | "error" | "offline" |
    "auth" | "unsupported" | "disabled" | "paused" | "not_installed";
};

/** The card image RomM keeps for a game: its size and the game saves inside it (`bytes` is the data they take). */
export type SaveUnit = { kind?: "folders" | "card" | "file"; size: number; contents: string[]; bytes: number };

export type SyncStatus = {
  rom_id: number; supported: boolean; enabled?: boolean; emulator?: string; data_dir?: string; data_dir_exists?: boolean;
  last?: { at: string; action: string; slot?: string; format?: string; unit?: SaveUnit | null } | null; conflict?: Conflict | null;
  history?: { at: string; action: string; detail: string }[];
  backups?: { name: string; path: string; size: number }[];
  slots?: { slot: number; enabled: boolean; name: string; kind: string }[];
};

export type RomDetail = {
  id: number; name: string; fs_name: string; platform: string; platform_slug: string; summary: string; size: number;
  regions: string[]; languages: string[]; genres: string[]; companies: string[];
  files: { name: string; size: number; category: string }[]; cover: string; installed: boolean;
  local: { main_file: string | null; path: string | null; size: number; installed_at: string } | null;
  download: DownloadJob | null; offline: string; saves: SyncStatus; saves_supported: boolean;
};

export type Volume = { path: string; name: string; total: number; free: number };

export type ProbeInfo = {
  url: string; version: string; supports_pairing: boolean; password_login: boolean; device_install: boolean;
};

export type DeviceFlow = { active: boolean; user_code?: string; verify_url?: string; seconds_left?: number };

export type Diagnostics = {
  version: string;
  helper: { ok: boolean; arch?: string; helper?: string; sigil?: string; error?: string };
  helper_tried: string[];
  host: { machine: string; kernel: string; user: string; home: string };
  paths: Record<string, string>;
  emulator: { name: string; data_dir: string; ini: string; ini_exists: boolean; executable: string; found_by: string };
  library_free: string;
  saves: { slot: string; tag: string; format: string; using: "folders" | "card"; server_version: string; server_units: boolean };
};

export type BiosFile = { name: string; size: number; kind: "bios" | "extra" };

/** One of RomM's firmware files, and what the emulator's folder says about it. */
export type BiosItem = {
  id: number; name: string; size: number; verified: boolean; md5: string; held_as: string;
  state: "missing" | "installed" | "present" | "different";
};

export type BiosRemote = {
  ok: boolean; platform: { id: number; name: string } | null; items: BiosItem[];
  reason: "" | "empty" | "no_platform" | "forbidden" | "auth" | "network" | string; message: string;
};

export type BiosStatus = {
  emulator: { name: string; id: string; running: boolean };
  dir: string; dir_exists: boolean; local: BiosFile[]; others: number; has_bios: boolean; remote: BiosRemote | null;
};

export type BiosReport = {
  installed: string[]; skipped: string[]; conflicts: string[]; busy: string[];
  failed: { name: string; error: string; kind: string }[];
};

/** bios_install answers with the report and the new status even when something failed, so it is not a plain Result. */
export type BiosInstallResult = {
  ok: boolean; message: string; error?: string; report?: BiosReport; status?: BiosStatus; dir?: string;
};
