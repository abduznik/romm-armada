import { ButtonItem, PanelSection, PanelSectionRow, TextField } from "@decky/ui";
import { ReactNode, useEffect, useRef, useState } from "react";
import { api } from "../api";
import { bytes, join, percent } from "../format";
import { downloads, useStore } from "../store";
import type { DownloadJob, Platform, RomSummary } from "../types";
import { BackBar, Muted, Nav, Note, useSingleFlight } from "./Common";

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
const MAX_REOPEN = 300;
/** How many games each list showed when it was left, so coming back from a game's page reopens it as long as it was. */
const shown = new Map<string, number>();

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
  const [marks, setMarks] = useState<number[]>([]);       // where each page added by "Show more" starts
  const [total, setTotal] = useState(0);
  const [search, setSearch] = useState("");
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  const dls = useStore(downloads);
  const seq = useRef(0);
  const itemsRef = useRef<RomSummary[]>([]);
  const marksRef = useRef<number[]>([]);
  const once = useSingleFlight();
  const memory = `${platformId ?? "all"}|${title}|${query}`;

  useEffect(() => {
    const t = window.setTimeout(() => setQuery(search.trim()), 450);
    return () => window.clearTimeout(t);
  }, [search]);

  const load = async (offset: number) => {
    const mine = ++seq.current;
    setBusy(true);
    try {
      const limit = offset === 0 ? Math.min(Math.max(PAGE, shown.get(memory) ?? 0), MAX_REOPEN) : PAGE;
      const r = installedOnly ? await api.installed(query, offset, limit) : await api.roms(platformId, query, offset, limit);
      if (mine !== seq.current) return;
      if (!r.ok) return setNote(r.message);
      const have = new Set(offset === 0 ? [] : itemsRef.current.map((x) => x.id));
      const fresh = r.items.filter((x) => !have.has(x.id));       // a list that moved on the server between pages repeats a game
      const next = offset === 0 ? fresh : [...itemsRef.current, ...fresh];
      const nextMarks = offset === 0 ? [] : fresh.length ? [...marksRef.current, itemsRef.current.length] : marksRef.current;
      itemsRef.current = next;
      marksRef.current = nextMarks;
      shown.set(memory, next.length);
      setItems(next);
      setMarks(nextMarks);
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

  // One keyed list for the games and the rows between pages. "Show more" is the row at the end; when its page arrives the same row
  // (same key, so the same element, still holding the controller focus) stays where it was, now above the new games, and a new one
  // closes the list. Pressing Down then simply goes on into the games that were just loaded.
  const rows: ReactNode[] = [];
  items.forEach((r, i) => {
    if (marks.includes(i)) {
      const end = marks.find((m) => m > i) ?? items.length;
      rows.push(
        <PanelSectionRow key={`more-${i}`}>
          <ButtonItem layout="below" description={`${end - i} more`}>
            {`Games ${i + 1}–${end}`}
          </ButtonItem>
        </PanelSectionRow>,
      );
    }
    rows.push(
      <PanelSectionRow key={`game-${r.id}`}>
        <ButtonItem
          layout="below"
          onClick={() => nav.push({ name: "game", id: r.id })}
          description={join([bytes(r.size), r.regions.join(", "), statusOf(r, dls[r.id])])}
        >
          {r.installed ? "● " : ""}
          {r.name}
        </ButtonItem>
      </PanelSectionRow>,
    );
  });
  if (items.length > 0 && total > PAGE) {
    rows.push(
      <PanelSectionRow key={`more-${items.length}`}>
        <ButtonItem layout="below" onClick={() => void once(async () => (items.length < total ? load(items.length) : undefined))}>
          {busy ? "Loading…" : items.length < total ? `Show more (${items.length} of ${total})` : `All ${total} games are shown`}
        </ButtonItem>
      </PanelSectionRow>,
    );
  }

  return (
    <>
      <BackBar nav={nav} title={title} />
      <PanelSection>
        <PanelSectionRow>
          <TextField label="Search" value={search} onChange={(e: { target: { value: string } }) => setSearch(e.target.value)} />
        </PanelSectionRow>
        {note ? <Note kind="warn">{note}</Note> : null}
        {rows}
        {!busy && items.length === 0 ? (
          <PanelSectionRow>
            <Muted>{query ? "Nothing matches." : "Nothing here yet."}</Muted>
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
