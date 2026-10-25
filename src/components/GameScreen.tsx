import { ButtonItem, ConfirmModal, PanelSection, PanelSectionRow, ToggleField, showModal } from "@decky/ui";
import { useState } from "react";
import { api } from "../api";
import { ago, bytes, eta, join, percent, speed } from "../format";
import { downloads, useStore } from "../store";
import type { RomDetail, SyncResult } from "../types";
import { Bar, BackBar, Info, Muted, Nav, Note, colors, usePolling } from "./Common";

export function confirm(title: string, text: string, ok: string, onOK: () => void, destructive = false): void {
  showModal(
    <ConfirmModal
      strTitle={title}
      strDescription={text}
      strOKButtonText={ok}
      strCancelButtonText="Cancel"
      bDestructiveWarning={destructive}
      onOK={onOK}
    />,
  );
}

export function GameScreen({ nav, id }: { nav: Nav; id: number }) {
  const [d, setD] = useState<RomDetail | null>(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState("");
  const [msg, setMsg] = useState<{ kind: "ok" | "warn" | "bad"; text: string } | null>(null);
  const dls = useStore(downloads);
  const job = dls[id] ?? d?.download ?? null;

  const load = async () => {
    const r = await api.rom(id);
    if (r.ok) {
      setD(r);
      setErr("");
    } else setErr(r.message);
  };
  const lastState = job?.state;
  usePolling(load, 8000, [id, lastState]);

  const show = (r: SyncResult) => {
    const kind = r.status === "uploaded" || r.status === "downloaded" || r.status === "up_to_date" ? "ok"
      : r.status === "conflict" || r.status === "busy" ? "warn" : "bad";
    setMsg({ kind, text: r.message });
    if (r.status === "conflict") nav.push({ name: "conflicts" });
  };

  const sync = async () => {
    setBusy("sync");
    setMsg(null);
    try {
      const r = await api.syncGame(id);
      if (r.ok) show(r);
      else setMsg({ kind: "bad", text: r.message });
    } finally {
      setBusy("");
      void load();
    }
  };

  if (!d) {
    return (
      <>
        <BackBar nav={nav} title="Game" />
        <PanelSection>
          {err ? <Note kind="bad">{err}</Note> : <Muted>Loading…</Muted>}
        </PanelSection>
      </>
    );
  }

  const active = job && ["queued", "running", "verifying"].includes(job.state);
  const saves = d.saves;
  const hasConflict = !!saves.conflict;

  return (
    <>
      <BackBar nav={nav} title={d.name} />
      <PanelSection>
        {d.cover ? (
          <PanelSectionRow>
            <img src={d.cover} alt="" style={{ maxWidth: "100%", maxHeight: 150, objectFit: "contain", borderRadius: 4, display: "block", margin: "0 auto" }} />
          </PanelSectionRow>
        ) : null}
        <Info label="Platform">{d.platform}</Info>
        <Info label="Size">{bytes(d.size)}</Info>
        {d.regions.length ? <Info label="Region">{d.regions.join(", ")}</Info> : null}
        {d.genres.length ? <Info label="Genre">{d.genres.slice(0, 3).join(", ")}</Info> : null}
        {d.offline ? <Note kind="warn">{d.offline}</Note> : null}
        {d.summary ? (
          <PanelSectionRow>
            <div style={{ fontSize: 12, color: colors.muted, lineHeight: 1.4, display: "-webkit-box", WebkitLineClamp: 5, WebkitBoxOrient: "vertical", overflow: "hidden" }}>
              {d.summary}
            </div>
          </PanelSectionRow>
        ) : null}
      </PanelSection>

      <PanelSection title="On this device">
        {d.installed && d.local ? (
          <>
            <Note kind="ok">Installed · {bytes(d.local.size)}</Note>
            <PanelSectionRow>
              <Muted>{d.local.path ?? d.local.main_file}</Muted>
            </PanelSectionRow>
          </>
        ) : null}
        {active && job ? (
          <>
            <PanelSectionRow>
              <div style={{ width: "100%" }}>
                <Bar value={percent(job.done, job.total)} indeterminate={job.state === "queued" || job.state === "verifying"} />
                <Muted>
                  {job.state === "queued" ? "Waiting for a free slot…" : job.state === "verifying" ? "Finishing up…" :
                    join([`${bytes(job.done)} of ${bytes(job.total)}`, speed(job.speed), eta(job.eta)])}
                </Muted>
              </div>
            </PanelSectionRow>
            <PanelSectionRow>
              <ButtonItem layout="below" onClick={() => void api.pause(id)}>
                Pause
              </ButtonItem>
            </PanelSectionRow>
            <PanelSectionRow>
              <ButtonItem layout="below" onClick={() => void api.cancel(id)}>
                Cancel download
              </ButtonItem>
            </PanelSectionRow>
          </>
        ) : null}
        {job && job.state === "paused" ? (
          <>
            <Note>
              Paused at {percent(job.done, job.total).toFixed(0)}%.
            </Note>
            <PanelSectionRow>
              <ButtonItem layout="below" onClick={() => void api.resume(id)}>
                Resume
              </ButtonItem>
            </PanelSectionRow>
            <PanelSectionRow>
              <ButtonItem layout="below" onClick={() => void api.cancel(id)}>
                Cancel download
              </ButtonItem>
            </PanelSectionRow>
          </>
        ) : null}
        {job && job.state === "error" ? (
          <>
            <Note kind="bad">{job.error}</Note>
            <PanelSectionRow>
              <ButtonItem layout="below" onClick={() => void api.resume(id)}>
                Try again
              </ButtonItem>
            </PanelSectionRow>
          </>
        ) : null}
        {!d.installed && !active && !(job && ["paused", "error"].includes(job.state)) ? (
          <PanelSectionRow>
            <ButtonItem
              layout="below"
              disabled={!!busy || !!d.offline}
              onClick={async () => {
                setBusy("install");
                const r = await api.install(id);
                setBusy("");
                if (!r.ok) setMsg({ kind: "bad", text: r.message });
                void load();
              }}
            >
              Download · {bytes(d.size)}
            </ButtonItem>
          </PanelSectionRow>
        ) : null}
        {d.installed ? (
          <PanelSectionRow>
            <ButtonItem
              layout="below"
              onClick={() =>
                confirm("Remove from this device?", `${d.name} is deleted from the games folder. Your saves and memory cards are not touched.`, "Remove", async () => {
                  const r = await api.uninstall(id);
                  if (!r.ok) setMsg({ kind: "bad", text: r.message });
                  void load();
                }, true)
              }
            >
              Remove from this device
            </ButtonItem>
          </PanelSectionRow>
        ) : null}
      </PanelSection>

      {d.installed && d.saves_supported ? (
        <PanelSection title="Saves">
          {!saves.supported ? <Note kind="warn">No emulator is set up for this game yet.</Note> : null}
          {hasConflict ? (
            <PanelSectionRow>
              <ButtonItem layout="below" onClick={() => nav.push({ name: "conflicts" })} description="Two different saves exist. Nothing was overwritten.">
                ⚠ Choose which save to keep
              </ButtonItem>
            </PanelSectionRow>
          ) : null}
          <Info label="Last sync">
            {saves.last ? ago(saves.last.at) + (saves.last.action === "download" && saves.last.slot ? ` · from ${saves.last.slot}` : "") : "never"}
          </Info>
          {saves.last?.unit && saves.last.unit.size > 0 ? (
            <>
              <Info label="RomM keeps">
                {`${bytes(saves.last.unit.size)} ${saves.last.unit.kind === "folders" ? "zip of save folders" : "card image"}: ${
                  saves.last.unit.contents.length
                    ? `${saves.last.unit.contents.join(", ")}, ${bytes(saves.last.unit.bytes)} of save data`
                    : "no saves"
                }`}
              </Info>
              <PanelSectionRow>
                <Muted>
                  {saves.last.unit.kind === "folders"
                    ? "RomM keeps this game's save folders in a zip, the way Argosy and Freegosy do. Only this game's folders are in it. The card on this device stays a folder card."
                    : "RomM keeps this game's save as a standard memory card image, the format RomM 5.5 and newer and sigil use. It holds only this game's save. The card on this device stays a folder card."}
                </Muted>
              </PanelSectionRow>
            </>
          ) : null}
          {saves.emulator ? <Info label="Emulator">{saves.emulator}</Info> : null}
          {saves.slots && saves.slots[0] ? (
            <Info label="Memory card">{`${saves.slots[0].name} (${saves.slots[0].kind})`}</Info>
          ) : null}
          <PanelSectionRow>
            <ButtonItem layout="below" disabled={!!busy || !saves.supported} onClick={sync}>
              {busy === "sync" ? "Syncing…" : "Sync this save now"}
            </ButtonItem>
          </PanelSectionRow>
          {msg ? <Note kind={msg.kind}>{msg.text}</Note> : null}
          <PanelSectionRow>
            <ToggleField
              label="Keep this game's save in sync"
              checked={saves.enabled !== false}
              onChange={(v: boolean) => {
                void api.gameSyncEnabled(id, v).then(load);
              }}
            />
          </PanelSectionRow>
          {saves.backups && saves.backups.length ? (
            <PanelSectionRow>
              <ButtonItem
                layout="below"
                description={`${saves.backups.length} kept, newest ${saves.backups[0].name.slice(0, 8)}`}
                onClick={() =>
                  confirm("Restore the memory card backup?", "The card goes back to how it was before the last restore. The current card is backed up first.", "Restore", async () => {
                    const r = await api.backupRestore(id, saves.backups![0].name);
                    setMsg({ kind: r.ok ? "ok" : "bad", text: r.ok ? r.message : r.message });
                    void load();
                  })
                }
              >
                Restore the latest backup
              </ButtonItem>
            </PanelSectionRow>
          ) : null}
          <PanelSectionRow>
            <ButtonItem
              layout="below"
              description="Forget what was last synced; the next sync asks before replacing anything"
              onClick={() => void api.gameSyncReset(id).then(load)}
            >
              Reset sync history
            </ButtonItem>
          </PanelSectionRow>
        </PanelSection>
      ) : null}

      {d.files.length ? (
        <PanelSection title="Files">
          {d.files.slice(0, 12).map((f) => (
            <PanelSectionRow key={f.name}>
              <Muted>
                {f.name} · {bytes(f.size)}
                {f.category !== "game" ? ` · ${f.category}` : ""}
              </Muted>
            </PanelSectionRow>
          ))}
        </PanelSection>
      ) : null}
    </>
  );
}
