import {
  ButtonItem, DropdownItem, Field, PanelSection, PanelSectionRow, SliderField, TextField, ToggleField,
} from "@decky/ui";
import { ReactNode, useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { Settings } from "../types";

export type Screen =
  | { name: "home" }
  | { name: "connect" }
  | { name: "platforms" }
  | { name: "games"; platformId: number | null; title: string }
  | { name: "game"; id: number }
  | { name: "downloads" }
  | { name: "conflicts" }
  | { name: "settings"; section?: "connection" | "library" | "saves" | "emulator" | "about" };

export type Nav = {
  push: (s: Screen) => void;
  pop: () => void;
  replace: (s: Screen) => void;
  home: () => void;
};

export const colors = { ok: "#59bf40", warn: "#e6b34a", bad: "#ff6b6b", muted: "#9aa4b2", info: "#66c0f4" };

/** TextField hands unknown props on to its <input>, but its typings do not list `placeholder`. */
export const placeholderProps = (text?: string): object => (text ? { placeholder: text } : {});

export function Muted({ children }: { children: ReactNode }) {
  return <div style={{ fontSize: 12, color: colors.muted, lineHeight: 1.35, overflowWrap: "anywhere" }}>{children}</div>;
}

export function Note({ kind = "info", children }: { kind?: "info" | "ok" | "warn" | "bad"; children: ReactNode }) {
  return (
    <PanelSectionRow>
      <div
        style={{
          fontSize: 13, lineHeight: 1.4, padding: "6px 8px", borderLeft: `3px solid ${colors[kind]}`,
          background: "rgba(255,255,255,0.04)", borderRadius: 3, overflowWrap: "anywhere",
        }}
      >
        {children}
      </div>
    </PanelSectionRow>
  );
}

export function BackBar({ nav, title }: { nav: Nav; title: string }) {
  return (
    <PanelSection title={title}>
      <PanelSectionRow>
        <ButtonItem layout="below" onClick={() => nav.pop()}>
          ‹ Back
        </ButtonItem>
      </PanelSectionRow>
    </PanelSection>
  );
}

export function Info({ label, children }: { label: string; children: ReactNode }) {
  return (
    <PanelSectionRow>
      <Field label={label} focusable={false} bottomSeparator="none">
        <div style={{ textAlign: "right", overflowWrap: "anywhere", maxWidth: 190, fontSize: 13 }}>{children}</div>
      </Field>
    </PanelSectionRow>
  );
}

export function Bar({ value, indeterminate = false, color }: { value: number; indeterminate?: boolean; color?: string }) {
  return (
    <div style={{ height: 6, borderRadius: 3, background: "rgba(255,255,255,0.12)", overflow: "hidden", width: "100%" }}>
      <div
        style={{
          height: "100%", width: indeterminate ? "35%" : `${Math.max(2, Math.min(100, value))}%`,
          background: color ?? colors.info, transition: "width 0.4s linear",
        }}
      />
    </div>
  );
}

/** Runs `fn` now and again every `ms`; cleans up. Errors are the caller's to handle. */
export function usePolling(fn: () => void | Promise<void>, ms: number, deps: unknown[] = []): void {
  const ref = useRef(fn);
  ref.current = fn;
  useEffect(() => {
    let live = true;
    const run = () => {
      if (live) void ref.current();
    };
    run();
    const t = window.setInterval(run, ms);
    return () => {
      live = false;
      window.clearInterval(t);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
}

// ---- settings controls, bound to a dotted path such as "saves.slot" ------------------------------------------

export function getPath(obj: unknown, path: string): any {
  return path.split(".").reduce((o: any, k) => (o == null ? undefined : o[k]), obj);
}

export function patchFor(path: string, value: unknown): Record<string, unknown> {
  const parts = path.split(".");
  const out: Record<string, unknown> = {};
  let node = out;
  parts.forEach((p, i) => {
    if (i === parts.length - 1) node[p] = value;
    else {
      const next: Record<string, unknown> = {};
      node[p] = next;
      node = next;
    }
  });
  return out;
}

export type SettingsApi = {
  s: Settings | null;
  error: string;
  set: (path: string, value: unknown) => Promise<boolean>;
  reload: () => Promise<void>;
};

export function useSettings(): SettingsApi {
  const [s, setS] = useState<Settings | null>(null);
  const [error, setError] = useState("");
  const reload = useCallback(async () => {
    const r = await api.settings();
    if (r) setS(r);
  }, []);
  useEffect(() => {
    void reload();
  }, [reload]);
  const set = useCallback(async (path: string, value: unknown) => {
    const r = await api.setSettings(patchFor(path, value));
    if (r.ok) {
      setS(r.settings);
      setError("");
      return true;
    }
    const msg = r.ok === false ? Object.values(r.errors ?? {})[0] ?? r.message : "";
    setError(`${path}: ${msg}`);
    return false;
  }, []);
  return { s, error, set, reload };
}

export function Toggle({ st, path, label, description }: { st: SettingsApi; path: string; label: string; description?: string }) {
  return (
    <PanelSectionRow>
      <ToggleField label={label} description={description} checked={!!getPath(st.s, path)} onChange={(v: boolean) => void st.set(path, v)} />
    </PanelSectionRow>
  );
}

export function Choice({
  st, path, label, options, description,
}: { st: SettingsApi; path: string; label: string; options: { value: string; label: string }[]; description?: string }) {
  const cur = getPath(st.s, path);
  return (
    <PanelSectionRow>
      <DropdownItem
        label={label}
        description={description}
        rgOptions={options.map((o) => ({ data: o.value, label: o.label }))}
        selectedOption={cur}
        onChange={(o: { data: string }) => void st.set(path, o.data)}
      />
    </PanelSectionRow>
  );
}

export function Num({
  st, path, label, min, max, step = 1, suffix, description, zeroLabel,
}: {
  st: SettingsApi; path: string; label: string; min: number; max: number; step?: number; suffix?: string;
  description?: string; zeroLabel?: string;
}) {
  const v = getPath(st.s, path);
  const [local, setLocal] = useState<number | null>(null);
  useEffect(() => setLocal(null), [v]);
  const shown = local ?? (typeof v === "number" ? v : min);
  return (
    <PanelSectionRow>
      <SliderField
        label={label}
        description={description}
        value={shown}
        min={min}
        max={max}
        step={step}
        showValue
        valueSuffix={zeroLabel && shown === 0 ? undefined : suffix}
        onChange={(n: number) => {
          setLocal(n);
          void st.set(path, n);
        }}
      />
      {zeroLabel && shown === 0 ? <Muted>{zeroLabel}</Muted> : null}
    </PanelSectionRow>
  );
}

export function Text({
  st, path, label, placeholder: hint, description, password,
}: { st: SettingsApi; path: string; label: string; placeholder?: string; description?: string; password?: boolean }) {
  const cur = String(getPath(st.s, path) ?? "");
  const [draft, setDraft] = useState<string | null>(null);
  const dirty = draft !== null && draft !== cur;
  return (
    <>
      <PanelSectionRow>
        <TextField
          label={label}
          description={description}
          value={draft ?? cur}
          {...placeholderProps(hint)}
          bIsPassword={password}
          onChange={(e: { target: { value: string } }) => setDraft(e.target.value)}
        />
      </PanelSectionRow>
      {dirty ? (
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            onClick={async () => {
              if (await st.set(path, draft)) setDraft(null);
            }}
          >
            Save {label.toLowerCase()}
          </ButtonItem>
        </PanelSectionRow>
      ) : null}
    </>
  );
}
