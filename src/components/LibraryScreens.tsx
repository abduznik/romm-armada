import { ButtonItem, PanelSection, PanelSectionRow, TextField } from "@decky/ui";
import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { bytes, join, percent } from "../format";
import { downloads, useStore } from "../store";
import type { DownloadJob, Platform, RomSummary } from "../types";
import { BackBar, Muted, Nav, Note } from "./Common";

export function PlatformsScreen({ nav }: { nav: Nav }) {
  const [list, setList] = useState<Platform[] | null>(null);
  const [stale, setStale] = useState(false);
  const [err, setErr] = useState("");
  useEffect(() => {
    void api.platforms().then((r) => {
      if (r.ok) {
        setList(r.platforms);
        setStale(r.stale);
      } else setErr(r.message);
    });
  }, []);
  return (
    <>
      <BackBar nav={nav} title="Library" />
      <PanelSection>
        {err ? <Note kind="bad">{err}</Note> : null}
        {stale ? <Note kind="warn">RomM cannot be reached. Showing what was seen last time.</Note> : null}
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => nav.push({ name: "games", platformId: null, title: "All games" })} description="Search every platform">
            All games
          </ButtonItem>
        </PanelSectionRow>
        {list === null && !err ? (
          <PanelSectionRow>
            <Muted>Loading…</Muted>
          </PanelSectionRow>
        ) : null}
        {(list ?? []).map((p) => (
          <PanelSectionRow key={p.id}>
            <ButtonItem
              layout="below"
              onClick={() => nav.push({ name: "games", platformId: p.id, title: p.name })}
              description={join([`${p.rom_count} games`, p.installed ? `${p.installed} installed` : "", p.saves ? "save sync" : "", `→ ${p.folder}/`])}
            >
              {p.name}
            </ButtonItem>
          </PanelSectionRow>
        ))}
      </PanelSection>
    </>
  );
}

const PAGE = 30;

function statusOf(r: RomSummary, job: DownloadJob | undefined): string {
  const j = job ?? r.download;
  if (j && ["queued", "running", "verifying"].includes(j.state)) return j.state === "queued" ? "waiting to download" : `downloading ${percent(j.done, j.total).toFixed(0)}%`;
  if (j && j.state === "paused") return "paused";
  if (j && j.state === "error") return "download failed";
  return r.installed ? "installed" : "";
}

export function GamesScreen({ nav, platformId, title }: { nav: Nav; platformId: number | null; title: string }) {
  const installedOnly = title === "Installed";
  const [items, setItems] = useState<RomSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [search, setSearch] = useState("");
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  const dls = useStore(downloads);
  const seq = useRef(0);

  useEffect(() => {
    const t = window.setTimeout(() => setQuery(search.trim()), 450);
    return () => window.clearTimeout(t);
  }, [search]);

  const load = async (offset: number) => {
    const mine = ++seq.current;
    setBusy(true);
    try {
      const r = installedOnly ? await api.installed(query, offset, PAGE) : await api.roms(platformId, query, offset, PAGE);
      if (mine !== seq.current) return;
      if (!r.ok) return setNote(r.message);
      setItems((cur) => (offset === 0 ? r.items : [...cur, ...r.items]));
      setTotal(r.total);
      setNote(r.stale ? r.message ?? "RomM cannot be reached. Showing installed games only." : "");
    } finally {
      if (mine === seq.current) setBusy(false);
    }
  };

  useEffect(() => {
    void load(0);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query, platformId]);

  return (
    <>
      <BackBar nav={nav} title={title} />
      <PanelSection>
        <PanelSectionRow>
          <TextField label="Search" value={search} onChange={(e: { target: { value: string } }) => setSearch(e.target.value)} />
        </PanelSectionRow>
        {note ? <Note kind="warn">{note}</Note> : null}
        {items.map((r) => (
          <PanelSectionRow key={r.id}>
            <ButtonItem
              layout="below"
              onClick={() => nav.push({ name: "game", id: r.id })}
              description={join([bytes(r.size), r.regions.join(", "), statusOf(r, dls[r.id])])}
            >
              {r.installed ? "● " : ""}
              {r.name}
            </ButtonItem>
          </PanelSectionRow>
        ))}
        {!busy && items.length === 0 ? (
          <PanelSectionRow>
            <Muted>{query ? "Nothing matches." : "Nothing here yet."}</Muted>
          </PanelSectionRow>
        ) : null}
        {items.length < total ? (
          <PanelSectionRow>
            <ButtonItem layout="below" disabled={busy} onClick={() => void load(items.length)}>
              {busy ? "Loading…" : `Show more (${items.length} of ${total})`}
            </ButtonItem>
          </PanelSectionRow>
        ) : null}
        {busy && items.length === 0 ? (
          <PanelSectionRow>
            <Muted>Loading…</Muted>
          </PanelSectionRow>
        ) : null}
      </PanelSection>
    </>
  );
}
