import { addEventListener, removeEventListener, toaster } from "@decky/api";
import { useSyncExternalStore } from "react";
import type { Conflict, DownloadJob, SyncResult } from "./types";

class Store<T> {
  private listeners = new Set<() => void>();
  constructor(private value: T) {}
  get = (): T => this.value;
  set(next: T): void {
    this.value = next;
    this.listeners.forEach((l) => l());
  }
  subscribe = (l: () => void): (() => void) => {
    this.listeners.add(l);
    return () => this.listeners.delete(l);
  };
}

export function useStore<T>(store: Store<T>): T {
  return useSyncExternalStore(store.subscribe, store.get);
}

/** Downloads by game id, kept current by the backend's progress events. */
export const downloads = new Store<Record<number, DownloadJob>>({});

/** Things the user has to look at; the home screen shows them. */
export const alerts = new Store<{ authRequired: boolean; conflicts: Record<number, Conflict>; emulatorRunning: boolean }>({
  authRequired: false,
  conflicts: {},
  emulatorRunning: false,
});

export function setDownloads(list: DownloadJob[]): void {
  const map: Record<number, DownloadJob> = {};
  for (const j of list) map[j.rom_id] = j;
  downloads.set(map);
}

function upsertDownload(job: DownloadJob): void {
  downloads.set({ ...downloads.get(), [job.rom_id]: job });
}

export function setConflicts(list: Conflict[]): void {
  const map: Record<number, Conflict> = {};
  for (const c of list) map[c.rom_id] = c;
  alerts.set({ ...alerts.get(), conflicts: map });
}

const toast = (title: string, body: string, critical = false) => {
  try {
    toaster.toast({ title, body, critical, duration: critical ? 8000 : 4000 });
  } catch {
    /* the toaster is not there in some contexts; nothing depends on it */
  }
};

/** What a finished sync is worth telling the user about while they are playing. */
export function describeSync(r: SyncResult): { title: string; body: string; critical: boolean } | null {
  const quiet = r.trigger === "manual" || !r.notify;
  switch (r.status) {
    case "uploaded":
      return quiet ? null : { title: "Save uploaded", body: r.name, critical: false };
    case "downloaded":
      return quiet ? null : { title: "Save updated from RomM", body: r.name, critical: false };
    case "conflict":
      return { title: "Choose which save to keep", body: `${r.name}: open RomM Armada`, critical: true };
    case "blocked":
      return r.notify ? { title: "Saves need attention", body: r.message, critical: false } : null;
    case "error":
      return r.trigger && r.trigger !== "manual" && r.notify ? { title: "Save sync failed", body: `${r.name}: ${r.message}`, critical: false } : null;
    default:
      return null;
  }
}

/** Subscribes to the backend's events. Returns the function that undoes it. */
export function startEvents(): () => void {
  const subs: [string, (...a: never[]) => void][] = [];
  const on = <A extends unknown[]>(event: string, fn: (...a: A) => void) => {
    const l = addEventListener<A>(event, fn);
    subs.push([event, l as unknown as (...a: never[]) => void]);
  };

  on<[DownloadJob]>("download_progress", (job) => {
    upsertDownload(job);
  });
  on<[{ rom_id: number; name: string }]>("installed", (e) => toast("Ready to play", e.name));
  on<[DownloadJob]>("download_progress", (job) => {
    if (job.state === "error") toast("Download failed", `${job.name}: ${job.error}`, true);
  });
  on<[SyncResult]>("sync_result", (r) => {
    if (r.status === "conflict" && r.conflict) alerts.set({ ...alerts.get(), conflicts: { ...alerts.get().conflicts, [r.rom_id]: r.conflict } });
    if ((r.status === "uploaded" || r.status === "downloaded") && alerts.get().conflicts[r.rom_id]) {
      const rest = { ...alerts.get().conflicts };
      delete rest[r.rom_id];
      alerts.set({ ...alerts.get(), conflicts: rest });
    }
    const t = describeSync(r);
    if (t) toast(t.title, t.body, t.critical);
  });
  on<[Conflict]>("conflict", (c) => alerts.set({ ...alerts.get(), conflicts: { ...alerts.get().conflicts, [c.rom_id]: c } }));
  on<[object]>("emulator_started", () => alerts.set({ ...alerts.get(), emulatorRunning: true }));
  on<[object]>("emulator_stopped", () => alerts.set({ ...alerts.get(), emulatorRunning: false }));
  on<[object]>("auth_required", () => {
    if (!alerts.get().authRequired) {
      alerts.set({ ...alerts.get(), authRequired: true });
      toast("RomM sign-in needed", "Open RomM Armada and pair this device again", true);
    }
  });

  return () => {
    for (const [event, l] of subs) removeEventListener(event, l as never);
  };
}
