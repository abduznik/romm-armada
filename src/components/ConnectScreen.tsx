import { ButtonItem, PanelSection, PanelSectionRow, TextField, ToggleField } from "@decky/ui";
import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { formatPairCode, pairCodeReady } from "../format";
import type { DeviceFlow, ProbeInfo } from "../types";
import { Muted, Note, colors, placeholderProps, usePolling } from "./Common";

type Props = { onConnected: () => void; reason?: string };

export function ConnectScreen({ onConnected, reason }: Props) {
  const [url, setUrl] = useState("");
  const [verify, setVerify] = useState(true);
  const [probe, setProbe] = useState<ProbeInfo | null>(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [warn, setWarn] = useState("");
  const [code, setCode] = useState("");
  const [flow, setFlow] = useState<DeviceFlow | null>(null);
  const [advanced, setAdvanced] = useState(false);
  const [token, setToken] = useState("");
  const [user, setUser] = useState("");
  const [pass, setPass] = useState("");
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    void api.settings().then((s) => {
      if (s && alive.current) {
        setUrl((u) => u || s.server.url);
        setVerify(s.server.verify_tls);
      }
    });
    return () => {
      alive.current = false;
    };
  }, []);

  const done = (r: { missing_scopes?: string[] }) => {
    if (r.missing_scopes && r.missing_scopes.length) {
      setWarn(`Connected, but this token lacks: ${r.missing_scopes.join(", ")}. Make a token with all permissions.`);
    }
    onConnected();
  };

  const run = async (what: string, fn: () => Promise<void>) => {
    setBusy(what);
    setError("");
    try {
      await fn();
    } catch (e) {
      setError(String((e as Error)?.message ?? e));
    } finally {
      if (alive.current) setBusy("");
    }
  };

  const check = () =>
    run("probe", async () => {
      const r = await api.probe(url, verify);
      if (!r.ok) return setError(r.message);
      setProbe(r);
      setUrl(r.url);
    });

  const pair = () =>
    run("code", async () => {
      const r = await api.pairWithCode(url, code, verify);
      if (!r.ok) {
        setCode(r.error === "bad_code" ? code : "");
        return setError(r.message);
      }
      done(r);
    });

  const startFlow = () =>
    run("flow", async () => {
      const r = await api.deviceStart(url, verify);
      if (!r.ok) return setError(r.message);
      setFlow(r);
    });

  usePolling(
    async () => {
      if (!flow?.active) return;
      const r = await api.devicePoll();
      if (!alive.current) return;
      if (r.status === "approved") {
        setFlow(null);
        done(r);
      } else if (r.status === "pending") {
        setFlow((f) => (f ? { ...f, seconds_left: r.seconds_left ?? f.seconds_left } : f));
      } else if (r.status === "denied" || r.status === "expired" || r.status === "none") {
        setFlow(null);
        setError(r.status === "denied" ? "The request was denied in RomM." : "That code ran out. Start again.");
      } else if (r.status === "error") {
        setError(r.message ?? "Could not ask RomM");
      }
    },
    3000,
    [flow?.active],
  );

  const reachable = !!probe;
  return (
    <>
      <PanelSection title="Connect to RomM">
        {reason ? <Note kind="warn">{reason}</Note> : null}
        {warn ? <Note kind="warn">{warn}</Note> : null}
        <PanelSectionRow>
          <TextField
            label="Server address"
            description="Like http://192.168.1.20:8080 or https://romm.example.com"
            value={url}
            {...placeholderProps("http://romm.local:8080")}
            onChange={(e: { target: { value: string } }) => {
              setUrl(e.target.value);
              setProbe(null);
            }}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem layout="below" disabled={!url.trim() || !!busy} onClick={check}>
            {busy === "probe" ? "Checking…" : reachable ? "Server reached again" : "Check the server"}
          </ButtonItem>
        </PanelSectionRow>
        {probe ? (
          <Note kind="ok">
            RomM {probe.version} found.{probe.supports_pairing ? "" : " This version cannot pair with a code; use a token below."}
          </Note>
        ) : null}
        {error ? <Note kind="bad">{error}</Note> : null}
      </PanelSection>

      <PanelSection title="Pair with a code">
        <PanelSectionRow>
          <Muted>
            In RomM's web page, open your API tokens, make a token with all permissions and choose Pair. Type the
            8-character code here within <b>60 seconds</b>; it works once.
          </Muted>
        </PanelSectionRow>
        <PanelSectionRow>
          <TextField
            label="Pairing code"
            value={code}
            {...placeholderProps("ABCD-EFGH")}
            onChange={(e: { target: { value: string } }) => setCode(formatPairCode(e.target.value))}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem layout="below" disabled={!url.trim() || !pairCodeReady(code) || !!busy} onClick={pair}>
            {busy === "code" ? "Pairing…" : "Pair this device"}
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="Or approve from another device">
        {flow?.active ? (
          <>
            <PanelSectionRow>
              <div style={{ textAlign: "center", padding: "6px 0" }}>
                <div style={{ fontSize: 30, letterSpacing: 4, fontWeight: 700, color: colors.info }}>{flow.user_code}</div>
                <Muted>Open this page on your phone or PC and approve:</Muted>
                <div style={{ fontSize: 12, overflowWrap: "anywhere", margin: "4px 0" }}>{flow.verify_url}</div>
                <Muted>{flow.seconds_left ? `Waiting… ${Math.floor((flow.seconds_left ?? 0) / 60)}:${String((flow.seconds_left ?? 0) % 60).padStart(2, "0")} left` : "Waiting…"}</Muted>
              </div>
            </PanelSectionRow>
            <PanelSectionRow>
              <ButtonItem
                layout="below"
                onClick={async () => {
                  await api.deviceCancel();
                  setFlow(null);
                }}
              >
                Cancel
              </ButtonItem>
            </PanelSectionRow>
          </>
        ) : (
          <>
            <PanelSectionRow>
              <Muted>This device shows a code; you approve it while signed in to RomM on your phone or PC.</Muted>
            </PanelSectionRow>
            <PanelSectionRow>
              <ButtonItem layout="below" disabled={!url.trim() || !!busy} onClick={startFlow}>
                {busy === "flow" ? "Asking RomM…" : "Show a code on this device"}
              </ButtonItem>
            </PanelSectionRow>
          </>
        )}
      </PanelSection>

      <PanelSection title="Other ways">
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => setAdvanced(!advanced)}>
            {advanced ? "Hide" : "Token, password, certificate…"}
          </ButtonItem>
        </PanelSectionRow>
        {advanced ? (
          <>
            <PanelSectionRow>
              <ToggleField
                label="Check the server's certificate"
                description="Turn off only for a server with a self-signed certificate"
                checked={verify}
                onChange={(v: boolean) => {
                  setVerify(v);
                  setProbe(null);
                }}
              />
            </PanelSectionRow>
            <PanelSectionRow>
              <TextField label="Client token" value={token} bIsPassword onChange={(e: { target: { value: string } }) => setToken(e.target.value)} />
            </PanelSectionRow>
            <PanelSectionRow>
              <ButtonItem
                layout="below"
                disabled={!url.trim() || !token.trim() || !!busy}
                onClick={() =>
                  run("token", async () => {
                    const r = await api.signInToken(url, token, verify);
                    if (!r.ok) return setError(r.message);
                    setToken("");
                    done(r);
                  })
                }
              >
                {busy === "token" ? "Signing in…" : "Use this token"}
              </ButtonItem>
            </PanelSectionRow>
            <PanelSectionRow>
              <Muted>A password is used once to make a token for this device, and is not kept.</Muted>
            </PanelSectionRow>
            <PanelSectionRow>
              <TextField label="Username" value={user} onChange={(e: { target: { value: string } }) => setUser(e.target.value)} />
            </PanelSectionRow>
            <PanelSectionRow>
              <TextField label="Password" value={pass} bIsPassword onChange={(e: { target: { value: string } }) => setPass(e.target.value)} />
            </PanelSectionRow>
            <PanelSectionRow>
              <ButtonItem
                layout="below"
                disabled={!url.trim() || !user || !pass || !!busy}
                onClick={() =>
                  run("password", async () => {
                    const r = await api.signInPassword(url, user, pass, verify);
                    setPass("");
                    if (!r.ok) return setError(r.message);
                    done(r);
                  })
                }
              >
                {busy === "password" ? "Signing in…" : "Sign in"}
              </ButtonItem>
            </PanelSectionRow>
          </>
        ) : null}
      </PanelSection>
    </>
  );
}
