export function bytes(n: number): string {
  if (!isFinite(n) || n < 0) return "–";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let v = n;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return i === 0 ? `${Math.round(v)} B` : `${v >= 100 ? v.toFixed(0) : v.toFixed(1)} ${units[i]}`;
}

export function speed(bps: number): string {
  return bps > 0 ? `${bytes(bps)}/s` : "";
}

export function eta(seconds: number): string {
  if (!seconds || seconds < 0) return "";
  if (seconds < 60) return `${Math.round(seconds)} s left`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} min left`;
  const h = Math.floor(seconds / 3600);
  return `${h} h ${Math.round((seconds % 3600) / 60)} min left`;
}

export function percent(done: number, total: number): number {
  if (!total) return 0;
  return Math.max(0, Math.min(100, (done / total) * 100));
}

/** "5 min ago", "yesterday", or the date. `now` is a parameter so tests need no clock. */
export function ago(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return "never";
  const t = Date.parse(iso);
  if (isNaN(t)) return "never";
  const s = Math.max(0, Math.round((now - t) / 1000));
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  if (s < 172800) return "yesterday";
  return new Date(t).toLocaleDateString();
}

export function join(parts: (string | false | null | undefined)[], sep = " · "): string {
  return parts.filter((p): p is string => !!p).join(sep);
}

/** The eight-character pairing code as the user types it: upper case, A-Z and 2-9 only, "XXXX-XXXX". */
export function formatPairCode(text: string): string {
  const clean = text.toUpperCase().replace(/[^A-Z0-9]/g, "").slice(0, 8);
  return clean.length > 4 ? `${clean.slice(0, 4)}-${clean.slice(4)}` : clean;
}

export function pairCodeReady(text: string): boolean {
  return text.replace(/[^A-Za-z0-9]/g, "").length === 8;
}

/** What each RomM permission is for, so a missing one is explained by what stops working. */
const SCOPE_USE: Record<string, string> = {
  "platforms.read": "browsing the library", "roms.read": "browsing and downloading games",
  "assets.read": "downloading saves", "assets.write": "uploading saves", "firmware.read": "downloading BIOS files",
  "devices.write": "registering this device", "devices.read": "registering this device",
  "roms.user.write": "play time", "roms.user.read": "play time",
};

export function describeScopes(missing: string[]): string {
  return missing.map((s) => (SCOPE_USE[s] ? `${s} (${SCOPE_USE[s]})` : s)).join(", ");
}
