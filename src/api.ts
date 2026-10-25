import { call } from "@decky/api";
import type {
  BiosInstallResult, BiosStatus, Conflict, Diagnostics, DeviceFlow, DownloadJob, Overview, Platform, ProbeInfo, Result,
  RomDetail, RomSummary, Settings, SyncResult, Volume,
} from "./types";

const rpc = <T>(method: string, ...args: unknown[]) => call<unknown[], T>(method, ...args);

type Items<T> = { items: T[] };

export const api = {
  overview: () => rpc<Result<Overview>>("overview"),
  diagnostics: () => rpc<Result<Diagnostics>>("diagnostics"),

  settings: () => rpc<Result<{ settings: Settings }>>("settings_get").then((r) => (r.ok ? (r as unknown as Settings) : null)),
  setSettings: (patch: Record<string, unknown>) =>
    rpc<Result<{ settings: Settings; changed: object }>>("settings_set", patch),
  checkLocation: (path: string) =>
    rpc<Result<{ path: string; exists: boolean; free: number; total: number; message: string }>>("check_location", path),
  volumes: () => rpc<Result<Items<Volume>>>("volumes"),

  probe: (url: string, verify: boolean) => rpc<Result<ProbeInfo>>("connect_probe", url, verify),
  pairWithCode: (url: string, code: string, verify: boolean) =>
    rpc<Result<{ username: string; device_id: string; missing_scopes: string[] }>>("connect_code", url, code, verify),
  deviceStart: (url: string, verify: boolean) => rpc<Result<DeviceFlow>>("connect_device_start", url, verify),
  devicePoll: () =>
    rpc<{ ok: boolean; status: string; message?: string; username?: string; missing_scopes?: string[] } & DeviceFlow>(
      "connect_device_poll"),
  deviceCancel: () => rpc<Result>("connect_device_cancel"),
  signInToken: (url: string, token: string, verify: boolean) =>
    rpc<Result<{ username: string; missing_scopes: string[] }>>("connect_token", url, token, verify),
  signInPassword: (url: string, user: string, password: string, verify: boolean) =>
    rpc<Result<{ username: string; missing_scopes: string[] }>>("connect_password", url, user, password, verify),
  disconnect: () => rpc<Result>("disconnect"),

  platforms: () => rpc<Result<{ platforms: Platform[]; stale: boolean }>>("platforms"),
  roms: (platformId: number | null, search: string, offset: number, limit: number) =>
    rpc<Result<{ items: RomSummary[]; total: number; offset: number; stale: boolean; message?: string }>>(
      "roms", platformId, search, offset, limit),
  installed: (search: string, offset: number, limit: number) =>
    rpc<Result<{ items: RomSummary[]; total: number; offset: number; stale: boolean; message?: string }>>(
      "installed", search, offset, limit),
  rom: (id: number) => rpc<Result<RomDetail>>("rom", id),
  install: (id: number) => rpc<Result<DownloadJob>>("install", id),
  uninstall: (id: number) => rpc<Result<{ removed: number }>>("uninstall", id),

  downloads: () => rpc<Result<Items<DownloadJob>>>("downloads"),
  pause: (id: number) => rpc<Result>("download_pause", id),
  resume: (id: number) => rpc<Result>("download_resume", id),
  cancel: (id: number) => rpc<Result>("download_cancel", id),
  clearDownloads: () => rpc<Result>("downloads_clear"),

  syncGame: (id: number, resolution?: string) => rpc<Result<SyncResult>>("sync_game", id, resolution ?? null),
  syncAll: () => rpc<Result<Items<SyncResult>>>("sync_all"),
  prepareCards: () => rpc<Result<{ message: string }>>("prepare_cards"),
  gameSyncEnabled: (id: number, enabled: boolean) => rpc<Result>("game_sync_enabled", id, enabled),
  gameSyncReset: (id: number) => rpc<Result>("game_sync_reset", id),
  conflicts: () => rpc<Result<Items<Conflict>>>("conflicts"),
  backupRestore: (id: number, name: string) => rpc<Result<{ message: string }>>("backup_restore", id, name),

  biosStatus: (remote = true) => rpc<Result<BiosStatus>>("bios_status", remote),
  /** ids: only these RomM firmware files (and `overwrite` may replace a different file of the same name); none = all missing. */
  biosInstall: (ids?: number[], overwrite = false) => rpc<BiosInstallResult>("bios_install", ids ?? null, overwrite),
};
