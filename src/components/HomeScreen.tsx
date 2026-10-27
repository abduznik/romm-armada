import { ButtonItem, PanelSection, PanelSectionRow } from "@decky/ui";
import { useState } from "react";
import { api } from "../api";
import { bytes, describeScopes, join } from "../format";
import { alerts, downloads, setConflicts, setDownloads, useStore } from "../store";
import type { Overview, SyncResult } from "../types";
import { Info, Muted, Nav, Note, usePolling } from "./Common";

function host(url: string): string {
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}

function summarize(results: SyncResult[]): string {
  if (!results.length) return "No installed games with save sync yet";
  const n = (s: string) => results.filter((r) => r.status === s).length;
  const failed = n("error") + n("offline") + n("blocked");
  const parts = [
    n("uploaded") > 0 && `${n("uploaded")} uploaded`,
    n("downloaded") > 0 && `${n("downloaded")} updated from RomM`,
    n("conflict") > 0 && `${n("conflict")} need a decision`,
    n("busy") > 0 && `${n("busy")} waiting for the emulator to close`,
    failed > 0 && `${failed} could not sync`,
  ];
  return join(parts) || "Everything is already in sync";
}

export function HomeScreen({ nav }: { nav: Nav }) {
  const [ov, setOv] = useState<Overview | null>(null);
  const [err, setErr] = useState("");
  const [syncing, setSyncing] = useState(false);
  const [syncMsg, setSyncMsg] = useState("");
  const [cardMsg, setCardMsg] = useState("");
  const al = useStore(alerts);
  const dls = useStore(downloads);

  const refresh = async () => {
    const r = await api.overview();
    if (r.ok) {
      setOv(r);
      setErr("");
      setConflicts(r.saves.conflicts);
    } else setErr(r.message);
    const d = await api.downloads();
    if (d.ok) setDownloads(d.items);
  };
  usePolling(refresh, 6000, []);

  const active = Object.values(dls).filter((j) => ["queued", "running", "verifying"].includes(j.state)).length;
  const failed = Object.values(dls).filter((j) => j.state === "error").length;
  const conflicts = Object.keys(al.conflicts).length;
  const em = ov?.emulator;
  const fileCard = em && em.card.wanted === "folder" && em.card.slot1.kind === "file";

  return (
    <>
      <PanelSection title="RomM">
        {err ? <Note kind="bad">{err}</Note> : null}
        {ov ? (
          <>
            <Info label="Server">{host(ov.server.url)}</Info>
            <Info label="Signed in as">{ov.server.username || "–"}</Info>
            <Info label="This device">{ov.device.name}</Info>
          </>
        ) : (
          <PanelSectionRow>
            <Muted>Loading…</Muted>
          </PanelSectionRow>
        )}
        {al.authRequired ? (
          <>
            <Note kind="bad">RomM no longer accepts this device. Pair it again.</Note>
            <PanelSectionRow>
              <ButtonItem layout="below" onClick={() => nav.push({ name: "connect" })}>
                Pair again
              </ButtonItem>
            </PanelSectionRow>
          </>
        ) : null}
        {conflicts > 0 ? (
          <PanelSectionRow>
            <ButtonItem layout="below" onClick={() => nav.push({ name: "conflicts" })} description="Two different saves exist. Nothing was overwritten.">
              ⚠ {conflicts} {conflicts === 1 ? "save needs" : "saves need"} a decision
            </ButtonItem>
          </PanelSectionRow>
        ) : null}
        {al.missingScopes.length > 0 ? (
          <>
            <Note kind="warn">
              Connected, but this sign-in cannot do everything: it lacks {describeScopes(al.missingScopes)}. Pair again and
              allow all of them.
            </Note>
            <PanelSectionRow>
              <ButtonItem layout="below" onClick={() => nav.push({ name: "connect" })}>
                Pair again
              </ButtonItem>
            </PanelSectionRow>
          </>
        ) : null}
        {ov && !ov.helper.ok ? <Note kind="bad">The save helper does not run on this device: {ov.helper.error}</Note> : null}
      </PanelSection>

      <PanelSection title="Games">
        {em && !em.bios.has_bios ? (
          <>
            <Note kind="warn">{em.name} has no BIOS yet, and cannot start a game without one.</Note>
            <PanelSectionRow>
              <ButtonItem layout="below" onClick={() => nav.push({ name: "bios" })}>
                Get the BIOS from RomM
              </ButtonItem>
            </PanelSectionRow>
          </>
        ) : null}
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => nav.push({ name: "platforms" })}>
            Browse the library
          </ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            onClick={() => nav.push({ name: "downloads" })}
            description={failed ? `${failed} failed` : undefined}
          >
            Downloads{active ? ` (${active} active)` : ""}
          </ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => nav.push({ name: "games", platformId: null, title: "Installed" })}>
            Installed on this device{ov ? ` (${ov.library.installed})` : ""}
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="Saves">
        {em?.running ? <Note kind="info">{em.name} is running. Saves sync when it closes.</Note> : null}
        {fileCard ? (
          <>
            <Note kind="warn">
              Memory card slot 1 ({em!.card.slot1.name}) is a file card. Folder cards are used for sync.
            </Note>
            <PanelSectionRow>
              <ButtonItem
                layout="below"
                onClick={async () => {
                  const r = await api.prepareCards();
                  setCardMsg(r.ok ? r.message : r.message);
                  void refresh();
                }}
              >
                Set up the memory card
              </ButtonItem>
            </PanelSectionRow>
            {cardMsg ? <Note>{cardMsg}</Note> : null}
          </>
        ) : null}
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            disabled={!ov?.saves.enabled}
            onClick={async () => {
              if (syncing) return;
              setSyncing(true);
              setSyncMsg("");
              try {
                const r = await api.syncAll();
                setSyncMsg(r.ok ? summarize(r.items) : r.message);
              } finally {
                setSyncing(false);
                void refresh();
              }
            }}
          >
            {syncing ? "Syncing…" : ov && !ov.saves.enabled ? "Save sync is off" : "Sync saves now"}
          </ButtonItem>
        </PanelSectionRow>
        {syncMsg ? <Note>{syncMsg}</Note> : null}
        {em ? (
          <Info label="Emulator">
            {em.name}
            {em.data_dir_exists ? "" : " (not run yet)"}
          </Info>
        ) : null}
        {ov?.dolphin && (ov.dolphin.present || ov.dolphin.games > 0) ? (
          <Info label="Dolphin">
            {ov.dolphin.games > 0 ? `${ov.dolphin.games} ${ov.dolphin.games === 1 ? "game" : "games"}` : "found"}
            {ov.dolphin.data_dir_exists ? "" : " (not run yet)"}
          </Info>
        ) : null}
        {ov?.retroarch && (ov.retroarch.present || ov.retroarch.games > 0) ? (
          <Info label="RetroArch">
            {ov.retroarch.games > 0 ? `${ov.retroarch.games} ${ov.retroarch.games === 1 ? "game" : "games"}` : "found"}
            {ov.retroarch.data_dir_exists ? "" : " (not run yet)"}
          </Info>
        ) : null}
      </PanelSection>

      <PanelSection title="This device">
        {ov ? (
          <>
            <Info label="Games go to">{ov.library.root}</Info>
            <Info label="Free space">{bytes(ov.library.free)}</Info>
            <Info label="Installed">{`${ov.library.installed} · ${bytes(ov.library.installed_size)}`}</Info>
          </>
        ) : null}
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => nav.push({ name: "settings" })}>
            Settings
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>
    </>
  );
}
