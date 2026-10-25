import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import { ConnectScreen } from "./components/ConnectScreen";
import type { Nav, Screen } from "./components/Common";
import { ConflictsScreen, DownloadsScreen } from "./components/DownloadsConflicts";
import { GameScreen } from "./components/GameScreen";
import { HomeScreen } from "./components/HomeScreen";
import { GamesScreen, PlatformsScreen } from "./components/LibraryScreens";
import { SettingsScreen } from "./components/SettingsScreen";
import { alerts, setConflicts, setDownloads, useStore } from "./store";

/** The panel: a stack of screens. Which one is at the bottom depends on whether the device is signed in. */
export function Root() {
  const [signedIn, setSignedIn] = useState<boolean | null>(null);
  const [stack, setStack] = useState<Screen[]>([{ name: "home" }]);
  const al = useStore(alerts);

  const refresh = useCallback(async () => {
    const r = await api.overview();
    if (r.ok) {
      setSignedIn(r.signed_in);
      setConflicts(r.saves.conflicts);
    } else setSignedIn(false);
    const d = await api.downloads();
    if (d.ok) setDownloads(d.items);
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const nav: Nav = {
    push: (s) => setStack((st) => [...st, s]),
    pop: () => setStack((st) => (st.length > 1 ? st.slice(0, -1) : st)),
    replace: (s) => setStack((st) => [...st.slice(0, -1), s]),
    home: () => setStack([{ name: "home" }]),
  };

  const connected = () => {
    alerts.set({ ...alerts.get(), authRequired: false });
    setSignedIn(true);
    nav.home();
    void refresh();
  };

  if (signedIn === null) return null;
  const top = stack[stack.length - 1];

  if (!signedIn || top.name === "connect") {
    return (
      <ConnectScreen
        onConnected={connected}
        reason={al.authRequired ? "RomM stopped accepting this device. Pair it again." : undefined}
      />
    );
  }

  switch (top.name) {
    case "platforms":
      return <PlatformsScreen nav={nav} />;
    case "games":
      return <GamesScreen key={`${top.platformId}-${top.title}`} nav={nav} platformId={top.platformId} title={top.title} />;
    case "game":
      return <GameScreen key={top.id} nav={nav} id={top.id} />;
    case "downloads":
      return <DownloadsScreen nav={nav} />;
    case "conflicts":
      return <ConflictsScreen nav={nav} />;
    case "settings":
      return (
        <SettingsScreen
          nav={nav}
          section={top.section}
          onDisconnected={() => {
            setSignedIn(false);
            nav.home();
          }}
        />
      );
    default:
      return <HomeScreen nav={nav} />;
  }
}
