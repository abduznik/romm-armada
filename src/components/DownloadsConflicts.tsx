import { ButtonItem, PanelSection, PanelSectionRow } from "@decky/ui";
import { useState } from "react";
import { api } from "../api";
import { ago, bytes, eta, percent, speed } from "../format";
import { alerts, downloads, setConflicts, setDownloads, useStore } from "../store";
import type { Conflict, DownloadJob } from "../types";
import { Bar, BackBar, Muted, Nav, Note, colors, usePolling } from "./Common";
import { confirm } from "./GameScreen";

const STATE_LABEL: Record<string, string> = {
  queued: "Waiting", running: "Downloading", verifying: "Finishing up", paused: "Paused", done: "Done",
  error: "Failed", cancelled: "Cancelled",
};

function JobRow({ job, nav }: { job: DownloadJob; nav: Nav }) {
  const live = ["queued", "running", "verifying"].includes(job.state);
  return (
    <PanelSection title={job.name}>
      <PanelSectionRow>
        <div style={{ width: "100%" }}>
          <Bar
            value={job.state === "done" ? 100 : percent(job.done, job.total)}
            indeterminate={job.state === "queued" || job.state === "verifying"}
            color={job.state === "error" ? colors.bad : job.state === "done" ? colors.ok : job.state === "paused" ? colors.warn : undefined}
          />
          <Muted>
            {STATE_LABEL[job.state] ?? job.state}
            {job.state === "running" ? ` · ${bytes(job.done)} of ${bytes(job.total)} ${speed(job.speed)} ${eta(job.eta)}` : ""}
            {job.state === "paused" ? ` at ${percent(job.done, job.total).toFixed(0)}%` : ""}
            {job.state === "done" ? ` · ${bytes(job.total)}` : ""}
          </Muted>
          {job.state === "error" ? <Muted>{job.error}</Muted> : null}
        </div>
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem layout="below" onClick={() => nav.push({ name: "game", id: job.rom_id })}>
          Open
        </ButtonItem>
      </PanelSectionRow>
      {live ? (
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => void api.pause(job.rom_id)}>
            Pause
          </ButtonItem>
        </PanelSectionRow>
      ) : null}
      {job.state === "paused" || job.state === "error" ? (
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => void api.resume(job.rom_id)}>
            {job.state === "error" ? "Try again" : "Resume"}
          </ButtonItem>
        </PanelSectionRow>
      ) : null}
      {live || job.state === "paused" ? (
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => void api.cancel(job.rom_id)}>
            Cancel
          </ButtonItem>
        </PanelSectionRow>
      ) : null}
    </PanelSection>
  );
}

export function DownloadsScreen({ nav }: { nav: Nav }) {
  const dls = useStore(downloads);
  usePolling(async () => {
    const r = await api.downloads();
    if (r.ok) setDownloads(r.items);
  }, 3000, []);
  const jobs = Object.values(dls).sort((a, b) => (b.created || "").localeCompare(a.created || ""));
  const finished = jobs.some((j) => ["done", "error", "cancelled"].includes(j.state));
  return (
    <>
      <BackBar nav={nav} title="Downloads" />
      {jobs.length === 0 ? (
        <PanelSection>
          <PanelSectionRow>
            <Muted>No downloads. Pick a game in the library to start one.</Muted>
          </PanelSectionRow>
        </PanelSection>
      ) : null}
      {jobs.map((j) => (
        <JobRow key={j.rom_id} job={j} nav={nav} />
      ))}
      {finished ? (
        <PanelSection>
          <PanelSectionRow>
            <ButtonItem layout="below" onClick={() => void api.clearDownloads().then(() => api.downloads()).then((r) => r.ok && setDownloads(r.items))}>
              Clear finished
            </ButtonItem>
          </PanelSectionRow>
        </PanelSection>
      ) : null}
    </>
  );
}

function ConflictCard({ c, onDone }: { c: Conflict; onDone: (msg: string) => void }) {
  const [busy, setBusy] = useState(false);
  const decide = (choice: "server" | "device" | "keep_both", title: string, text: string) =>
    confirm(title, text, "Do it", async () => {
      setBusy(true);
      try {
        const r = await api.syncGame(c.rom_id, choice);
        onDone(r.ok ? `${c.name}: ${r.message}` : r.message);
      } finally {
        setBusy(false);
      }
    });
  const why = c.reason === "first_contact"
    ? "This device has never synced this game, and RomM already has a different save."
    : "The save changed here and on RomM since they last matched.";
  return (
    <PanelSection title={c.name}>
      <Note kind="warn">{why}</Note>
      <PanelSectionRow>
        <Muted>
          This device: {c.local.has_saves ? ago(c.local.updated_at) : "no save"} · RomM: {ago(c.server.updated_at)}
          {c.server.device ? ` (from ${c.server.device})` : ""}
          {c.server.slot ? ` · slot ${c.server.slot}` : ""}
        </Muted>
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem
          layout="below"
          disabled={busy}
          description="Replaces the save on this device. It is backed up first."
          onClick={() => decide("server", "Use RomM's save?", `${c.name}: the save on this device is replaced by the one on RomM. A backup of the memory card is kept.`)}
        >
          Keep RomM's save
        </ButtonItem>
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem
          layout="below"
          disabled={busy || !c.local.has_saves}
          description="Uploads this device's save as the newest on RomM."
          onClick={() => decide("device", "Use this device's save?", `${c.name}: this device's save becomes the newest on RomM. The older one stays in its history.`)}
        >
          Keep this device's save
        </ButtonItem>
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem
          layout="below"
          disabled={busy || !c.local.has_saves}
          description="Stores this device's save in its own slot on RomM, then uses RomM's."
          onClick={() => decide("keep_both", "Keep both?", `${c.name}: this device's save is stored in a separate slot on RomM, then RomM's save is restored here.`)}
        >
          Keep both
        </ButtonItem>
      </PanelSectionRow>
    </PanelSection>
  );
}

export function ConflictsScreen({ nav }: { nav: Nav }) {
  const al = useStore(alerts);
  const [msg, setMsg] = useState("");
  usePolling(async () => {
    const r = await api.conflicts();
    if (r.ok) setConflicts(r.items);
  }, 5000, []);
  const list = Object.values(al.conflicts);
  return (
    <>
      <BackBar nav={nav} title="Saves to decide" />
      {msg ? (
        <PanelSection>
          <Note>{msg}</Note>
        </PanelSection>
      ) : null}
      {list.length === 0 ? (
        <PanelSection>
          <PanelSectionRow>
            <Muted>Nothing to decide. Every save matches RomM.</Muted>
          </PanelSectionRow>
        </PanelSection>
      ) : null}
      {list.map((c) => (
        <ConflictCard
          key={c.rom_id}
          c={c}
          onDone={(m) => {
            setMsg(m);
            void api.conflicts().then((r) => r.ok && setConflicts(r.items));
          }}
        />
      ))}
    </>
  );
}
