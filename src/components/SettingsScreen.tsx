import { FileSelectionType, openFilePicker } from "@decky/api";
import { ButtonItem, DropdownItem, PanelSection, PanelSectionRow } from "@decky/ui";
import { useEffect, useState } from "react";
import { api } from "../api";
import { bytes } from "../format";
import type { Diagnostics, Overview, Volume } from "../types";
import {
  BackBar, Choice, Info, Muted, Nav, Note, Num, Text, Toggle, usePolling, useSettings, SettingsApi,
} from "./Common";
import { confirm } from "./GameScreen";

type Section = "connection" | "library" | "saves" | "emulator" | "about";

export function SettingsScreen({ nav, section, onDisconnected }: { nav: Nav; section?: Section; onDisconnected: () => void }) {
  if (!section) {
    return (
      <>
        <BackBar nav={nav} title="Settings" />
        <PanelSection>
          {([
            ["library", "Games and downloads", "Where games go, folder names, download behaviour"],
            ["saves", "Saves and memory card", "When saves sync, folder memory cards, backups"],
            ["emulator", "Emulator", "ARMSX2 or PCSX2, and where its data is"],
            ["connection", "Connection", "Server, device name, disconnect"],
            ["about", "About and diagnostics", "Versions, the save helper, paths"],
          ] as [Section, string, string][]).map(([key, label, desc]) => (
            <PanelSectionRow key={key}>
              <ButtonItem layout="below" description={desc} onClick={() => nav.push({ name: "settings", section: key })}>
                {label}
              </ButtonItem>
            </PanelSectionRow>
          ))}
        </PanelSection>
      </>
    );
  }
  switch (section) {
    case "library":
      return <LibrarySettings nav={nav} />;
    case "saves":
      return <SavesSettings nav={nav} />;
    case "emulator":
      return <EmulatorSettings nav={nav} />;
    case "connection":
      return <ConnectionSettings nav={nav} onDisconnected={onDisconnected} />;
    default:
      return <AboutScreen nav={nav} />;
  }
}

// ---- library --------------------------------------------------------------------------------------------------

function LocationSetting({ st }: { st: SettingsApi }) {
  const [vols, setVols] = useState<Volume[]>([]);
  const [msg, setMsg] = useState<{ kind: "ok" | "bad"; text: string } | null>(null);
  useEffect(() => {
    void api.volumes().then((r) => r.ok && setVols(r.items));
  }, []);
  const root = st.s?.library.root ?? "";

  const apply = async (path: string) => {
    const c = await api.checkLocation(path);
    if (!c.ok) return setMsg({ kind: "bad", text: c.message });
    if (await st.set("library.root", c.path)) setMsg({ kind: "ok", text: `Games now go to ${c.path} (${bytes(c.free)} free).` });
  };

  const browse = async () => {
    try {
      const picked = await openFilePicker(FileSelectionType.FOLDER, root || "/", false, true);
      if (picked?.path) await apply(picked.path);
    } catch {
      /* closed without choosing */
    }
  };

  const options = vols.map((v) => ({
    data: v.path.endsWith("/ROMs") ? v.path : `${v.path.replace(/\/$/, "")}/ROMs`,
    label: `${v.name} (${bytes(v.free)} free)`,
  }));
  return (
    <>
      <Info label="Download location">{root}</Info>
      {options.length > 1 ? (
        <PanelSectionRow>
          <DropdownItem
            label="Choose a drive"
            description="Games are stored in a ROMs folder on it"
            rgOptions={options.map((o) => ({ data: o.data, label: o.label }))}
            selectedOption={options.find((o) => o.data === root)?.data}
            strDefaultLabel="Pick a drive"
            onChange={(o: { data: string }) => void apply(o.data)}
          />
        </PanelSectionRow>
      ) : null}
      <PanelSectionRow>
        <ButtonItem layout="below" onClick={browse}>
          Choose another folder…
        </ButtonItem>
      </PanelSectionRow>
      <Text st={st} path="library.root" label="Folder path" placeholder="/run/media/mmcblk0p1/ROMs" />
      {msg ? <Note kind={msg.kind}>{msg.text}</Note> : null}
    </>
  );
}

function LibrarySettings({ nav }: { nav: Nav }) {
  const st = useSettings();
  return (
    <>
      <BackBar nav={nav} title="Games and downloads" />
      <PanelSection title="Where games go">
        {st.error ? <Note kind="bad">{st.error}</Note> : null}
        {st.s ? <LocationSetting st={st} /> : <Muted>Loading…</Muted>}
        <Choice
          st={st}
          path="library.folder_style"
          label="Folder names"
          description="ES-DE names (ps2, psx, gc…) let ES-DE find the games with no setup"
          options={[{ value: "esde", label: "ES-DE names" }, { value: "romm", label: "As named in RomM" }]}
        />
      </PanelSection>
      <PanelSection title="Downloading">
        <Toggle st={st} path="library.include_extras" label="Include updates and DLC" description="Manuals and soundtracks are never downloaded" />
        <Choice
          st={st}
          path="library.verify"
          label="Check downloads"
          options={[
            { value: "crc", label: "Checksum (recommended)" },
            { value: "size", label: "Size only" },
            { value: "none", label: "Not at all" },
          ]}
        />
        <Num st={st} path="library.max_concurrent" label="Downloads at once" min={1} max={3} />
        <Num st={st} path="library.speed_limit_kbs" label="Speed limit" min={0} max={30000} step={500} suffix=" KB/s" zeroLabel="No limit" />
        <Num st={st} path="library.min_free_mb" label="Keep this much free" min={0} max={20000} step={256} suffix=" MB" description="A download that would use it is refused" />
        <Toggle st={st} path="library.resume_on_start" label="Continue unfinished downloads" description="After a restart or a wake from sleep" />
        <Toggle st={st} path="library.extract_zip" label="Unpack .zip games" description="For emulators that cannot read zip files" />
        <Toggle st={st} path="library.remote_installs" label="Accept games sent from RomM" description="Use 'install on device' in RomM's web page" />
      </PanelSection>
    </>
  );
}

// ---- saves ------------------------------------------------------------------------------------------------------

function SavesSettings({ nav }: { nav: Nav }) {
  const st = useSettings();
  const [ov, setOv] = useState<Overview | null>(null);
  const [msg, setMsg] = useState<{ kind: "ok" | "bad"; text: string } | null>(null);
  usePolling(async () => {
    const r = await api.overview();
    if (r.ok) setOv(r);
  }, 5000, []);
  const card = ov?.emulator.card;
  return (
    <>
      <BackBar nav={nav} title="Saves and memory card" />
      <PanelSection title="Sync">
        {st.error ? <Note kind="bad">{st.error}</Note> : null}
        <Toggle st={st} path="saves.enabled" label="Sync saves with RomM" />
        <Toggle st={st} path="saves.push_on_exit" label="Upload when the emulator closes" />
        <Toggle st={st} path="saves.pull_on_startup" label="Check for newer saves at start" />
        <Toggle st={st} path="saves.pull_when_idle" label="Bring down newer saves" description="Only while no emulator is running" />
        <Num st={st} path="saves.pull_interval_min" label="Check every" min={0} max={240} step={5} suffix=" min" zeroLabel="Only at start and after waking" />
        <Choice
          st={st}
          path="saves.conflict"
          label="If both sides changed"
          description="Nothing is overwritten unless you chose it here or in the question"
          options={[
            { value: "ask", label: "Ask me" },
            { value: "server", label: "Keep RomM's save" },
            { value: "device", label: "Keep this device's save" },
            { value: "newest", label: "Keep the newer one" },
          ]}
        />
        <Info label="Slot on RomM">{st.s?.saves.slot ?? ""}</Info>
        <Text
          st={st}
          path="saves.slot"
          label="Slot name"
          description="What this device's saves are called on RomM. The newest save in any slot is still pulled, whoever made it"
        />
        <Choice
          st={st}
          path="saves.format"
          label="Save format on RomM"
          description="Save folders: a zip of the game's own folders, as Freegosy and Argosy keep them. A saved .ps2 on RomM is still read and only the game's part is taken"
          options={[
            { value: "folders", label: "Save folders (zip)" },
            { value: "auto", label: "Automatic (card image on RomM 5.5 and newer)" },
            { value: "card", label: "Card image (.ps2)" },
          ]}
        />
      </PanelSection>

      <PanelSection title="PlayStation 2 memory card">
        <Choice
          st={st}
          path="saves.card_type"
          label="Card type"
          description="Folder cards keep each game's save in its own folder, so a sync only writes that game"
          options={[
            { value: "folder", label: "Folder card (recommended)" },
            { value: "auto", label: "Use what the emulator has" },
          ]}
        />
        {card ? (
          <Info label="Slot 1">
            {card.slot1.enabled ? `${card.slot1.name} · ${card.slot1.kind === "missing" ? "not created yet" : card.slot1.kind + " card"}` : "turned off in the emulator"}
          </Info>
        ) : null}
        {card && card.wanted === "folder" && card.slot1.kind === "file" ? (
          <Note kind="warn">
            Slot 1 is a file card. Convert it in the emulator (Settings, Memory Cards) to a folder card, or choose "Use what the emulator has".
          </Note>
        ) : null}
        {card?.custom_dir ? <Note kind="warn">The emulator keeps its memory cards outside its data folder; the plugin cannot follow that.</Note> : null}
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            disabled={!!ov?.emulator.running}
            description="Creates the folder card if there is none. An existing card with saves is never replaced."
            onClick={async () => {
              const r = await api.prepareCards();
              setMsg({ kind: r.ok ? "ok" : "bad", text: r.message });
              const o = await api.overview();
              if (o.ok) setOv(o);
            }}
          >
            Set up the memory card
          </ButtonItem>
        </PanelSectionRow>
        {msg ? <Note kind={msg.kind}>{msg.text}</Note> : null}
      </PanelSection>

      <PanelSection title="Safety">
        <Toggle st={st} path="saves.backups" label="Back up the card before restoring" description="Nothing is written to a card without a copy first" />
        <Num st={st} path="saves.backup_keep" label="Backups to keep per game" min={1} max={20} />
        <Toggle st={st} path="saves.autocleanup" label="Keep only recent versions on RomM" />
        <Num st={st} path="saves.autocleanup_limit" label="Versions on RomM" min={1} max={50} />
      </PanelSection>

      <PanelSection title="Extras">
        <Toggle st={st} path="saves.playtime" label="Send play time to RomM" />
        <Toggle st={st} path="saves.notify" label="Show notifications" description="Uploads, updates and decisions needed" />
      </PanelSection>
    </>
  );
}

// ---- emulator -----------------------------------------------------------------------------------------------------

function EmulatorSettings({ nav }: { nav: Nav }) {
  const st = useSettings();
  const [ov, setOv] = useState<Overview | null>(null);
  usePolling(async () => {
    const r = await api.overview();
    if (r.ok) setOv(r);
  }, 5000, [st.s?.emulators.ps2.emulator, st.s?.emulators.ps2.data_dir]);
  const em = ov?.emulator;
  return (
    <>
      <BackBar nav={nav} title="Emulator" />
      <PanelSection title="PlayStation 2">
        {st.error ? <Note kind="bad">{st.error}</Note> : null}
        <Choice
          st={st}
          path="emulators.ps2.emulator"
          label="Emulator"
          options={[
            { value: "auto", label: "Detect (ARMSX2 first)" },
            { value: "armsx2", label: "ARMSX2" },
            { value: "pcsx2", label: "PCSX2" },
          ]}
        />
        {em ? (
          <>
            <Info label="Using">{em.name}</Info>
            <Info label="Data folder">{em.data_dir}</Info>
            <Info label="Found">{em.data_dir_exists ? (em.found_by === "setting" ? "set below" : "yes") : "not yet; run the emulator once"}</Info>
            <Info label="Program">{em.executable || "not found"}</Info>
          </>
        ) : null}
        <Text st={st} path="emulators.ps2.data_dir" label="Data folder" placeholder="empty = detect (~/.config/ARMSX2)" description="Only if you moved it, or started the emulator with -datapath" />
        <Text st={st} path="emulators.ps2.executable" label="Program" placeholder="empty = detect" />
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            onClick={() => nav.push({ name: "bios" })}
            description={em ? (em.bios.has_bios ? `${em.bios.count} in ${em.bios.dir}` : `none in ${em.bios.dir}`) : undefined}
          >
            BIOS files
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>
    </>
  );
}

// ---- connection -----------------------------------------------------------------------------------------------------

function ConnectionSettings({ nav, onDisconnected }: { nav: Nav; onDisconnected: () => void }) {
  const st = useSettings();
  return (
    <>
      <BackBar nav={nav} title="Connection" />
      <PanelSection>
        {st.error ? <Note kind="bad">{st.error}</Note> : null}
        <Info label="Server">{st.s?.server.url ?? ""}</Info>
        <Info label="Signed in as">{st.s?.auth.username || "–"}</Info>
        <Info label="Signed in by">{st.s?.auth.method || "–"}</Info>
        <Text st={st} path="auth.device_name" label="This device is called" description="Shown in RomM next to this device's saves" />
        <Toggle st={st} path="server.verify_tls" label="Check the server's certificate" />
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => nav.push({ name: "connect" })}>
            Pair again
          </ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            onClick={() =>
              confirm("Disconnect from RomM?", "Downloaded games and saves stay on this device. You can pair again at any time.", "Disconnect", async () => {
                await api.disconnect();
                onDisconnected();
              }, true)
            }
          >
            Disconnect
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>
    </>
  );
}

// ---- about --------------------------------------------------------------------------------------------------------------

function AboutScreen({ nav }: { nav: Nav }) {
  const [d, setD] = useState<Diagnostics | null>(null);
  const [err, setErr] = useState("");
  useEffect(() => {
    void api.diagnostics().then((r) => (r.ok ? setD(r) : setErr(r.message)));
  }, []);
  return (
    <>
      <BackBar nav={nav} title="About" />
      <PanelSection>
        {err ? <Note kind="bad">{err}</Note> : null}
        {d ? (
          <>
            <Info label="Plugin">{d.version}</Info>
            <Info label="Save helper">{d.helper.ok ? `${d.helper.helper} · ${d.helper.arch}` : "not running"}</Info>
            <Info label="Built on">{d.helper.sigil ? `argosy-sigil ${d.helper.sigil}` : "–"}</Info>
            {!d.helper.ok ? <Note kind="bad">{d.helper.error}</Note> : null}
            {d.helper_tried.length ? <Muted>{d.helper_tried.join(" · ")}</Muted> : null}
            <Info label="Device">{`${d.host.machine} · ${d.host.kernel}`}</Info>
            <Info label="Running as">{d.host.user || "–"}</Info>
            <Info label="RomM server">{d.saves.server_version || "not asked yet"}</Info>
            <Info label="Saves go to RomM as">
              {d.saves.using === "folders" ? "a zip of the game's save folders" : "a memory card image"} · slot {d.saves.slot}
            </Info>
            <Info label="Emulator data">{d.emulator.data_dir}</Info>
            <Info label="Settings">{d.paths.settings}</Info>
            <Info label="Backups">{d.paths.backups}</Info>
            <Info label="Logs">{d.paths.logs}</Info>
            <Info label="Games folder free">{d.library_free}</Info>
          </>
        ) : null}
      </PanelSection>
    </>
  );
}
