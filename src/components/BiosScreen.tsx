import { ButtonItem, PanelSection, PanelSectionRow } from "@decky/ui";
import { useEffect, useState } from "react";
import { api } from "../api";
import { bytes, join } from "../format";
import type { BiosItem, BiosStatus } from "../types";
import { BackBar, Info, Muted, Nav, Note } from "./Common";
import { confirm } from "./GameScreen";

const STATE_TEXT: Record<BiosItem["state"], string> = {
  missing: "not in the folder",
  installed: "in the folder",
  present: "in the folder under another name",
  different: "a different file has this name",
};

const KIND_TEXT: Record<string, string> = { bios: "", extra: " (goes with a BIOS)", other: " (not a BIOS image)" };

/** BIOS files: what the emulator's folder holds, and what RomM keeps for the platform. Nothing is placed unless asked. */
export function BiosScreen({ nav }: { nav: Nav }) {
  const [st, setSt] = useState<BiosStatus | null>(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ kind: "ok" | "warn" | "bad"; text: string } | null>(null);

  const load = async () => {
    const r = await api.biosStatus(true);
    if (r.ok) {
      setSt(r);
      setErr("");
    } else setErr(r.message);
  };
  useEffect(() => {
    void load();
  }, []);

  const run = async (ids?: number[], overwrite = false) => {
    setBusy(true);
    setMsg(null);
    try {
      const r = await api.biosInstall(ids, overwrite);
      if (r.status) setSt(r.status);
      const failed = (r.report?.failed.length ?? 0) > 0 || (r.report?.busy.length ?? 0) > 0;
      setMsg({ kind: r.ok ? "ok" : failed || r.error ? "bad" : "warn", text: r.message });
    } finally {
      setBusy(false);
    }
  };

  const remote = st?.remote ?? null;
  const missing = remote?.items.filter((i) => i.state === "missing") ?? [];

  const pick = (i: BiosItem) => {
    if (i.state === "missing") void run([i.id]);
    else if (i.state === "different")
      confirm(
        `Replace ${i.name}?`,
        `A different file with this name is in the folder. It is copied to the plugin's backups first, then replaced with RomM's.`,
        "Replace",
        () => void run([i.id], true),
      );
  };

  return (
    <>
      <BackBar nav={nav} title="BIOS files" />
      {err ? (
        <PanelSection>
          <Note kind="bad">{err}</Note>
        </PanelSection>
      ) : null}
      {st ? (
        <>
          <PanelSection title={`In ${st.emulator.name}'s folder`}>
            <Info label="Folder">{st.dir}</Info>
            {!st.dir_exists ? (
              <PanelSectionRow>
                <Muted>The folder does not exist yet. Installing creates it.</Muted>
              </PanelSectionRow>
            ) : null}
            {st.local.length === 0 ? (
              <PanelSectionRow>
                <Muted>No files in it.</Muted>
              </PanelSectionRow>
            ) : (
              st.local.map((f) => (
                <Info key={f.name} label={f.name}>
                  {bytes(f.size)}
                  {KIND_TEXT[f.kind] ?? ""}
                </Info>
              ))
            )}
            {!st.has_bios ? <Note kind="warn">{st.emulator.name} cannot start a game without a BIOS image.</Note> : null}
            {st.emulator.running ? <Note>{st.emulator.name} is running. Files can be added now, not replaced.</Note> : null}
          </PanelSection>

          <PanelSection title={remote?.platform ? `From RomM (${remote.platform.name})` : "From RomM"}>
            {remote && !remote.ok ? (
              <>
                <Note kind="bad">{remote.message}</Note>
                {remote.reason === "forbidden" || remote.reason === "auth" ? (
                  <PanelSectionRow>
                    <ButtonItem layout="below" onClick={() => nav.push({ name: "connect" })}>
                      Pair again
                    </ButtonItem>
                  </PanelSectionRow>
                ) : null}
              </>
            ) : null}
            {remote && remote.ok && remote.items.length === 0 ? <Note>{remote.message}</Note> : null}
            {remote && remote.ok && remote.items.length > 0 ? (
              <>
                <PanelSectionRow>
                  <ButtonItem layout="below" disabled={busy || missing.length === 0} onClick={() => void run()}>
                    {busy
                      ? "Installing…"
                      : missing.length === 0
                        ? "Everything from RomM is in the folder"
                        : `Install ${missing.length} missing ${missing.length === 1 ? "file" : "files"}`}
                  </ButtonItem>
                </PanelSectionRow>
                {remote.items.map((i) => (
                  <PanelSectionRow key={i.id}>
                    <ButtonItem
                      layout="below"
                      disabled={busy || i.state === "installed" || i.state === "present"}
                      description={join([bytes(i.size), i.verified && "verified by RomM", STATE_TEXT[i.state] + (i.state === "present" ? ` (${i.held_as})` : "")])}
                      onClick={() => pick(i)}
                    >
                      {i.state === "installed" || i.state === "present" ? "✓ " : ""}
                      {i.name}
                    </ButtonItem>
                  </PanelSectionRow>
                ))}
              </>
            ) : null}
            {msg ? <Note kind={msg.kind}>{msg.text}</Note> : null}
            <PanelSectionRow>
              <ButtonItem layout="below" disabled={busy} onClick={() => void load()}>
                Check again
              </ButtonItem>
            </PanelSectionRow>
          </PanelSection>
        </>
      ) : err ? null : (
        <PanelSection>
          <PanelSectionRow>
            <Muted>Loading…</Muted>
          </PanelSectionRow>
        </PanelSection>
      )}
    </>
  );
}
