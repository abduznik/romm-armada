import { definePlugin } from "@decky/api";
import { staticClasses } from "@decky/ui";
import { Root } from "./Root";
import { startEvents } from "./store";

function Icon() {
  // a game disc with a down arrow: the library coming to the handheld
  return (
    <svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="9" />
      <circle cx="12" cy="12" r="2.2" />
      <path d="M12 3.5v4" />
      <path d="M8.5 17.5 12 21l3.5-3.5" opacity="0" />
    </svg>
  );
}

export default definePlugin(() => {
  const stop = startEvents();
  return {
    name: "RomM Armada",
    titleView: <div className={staticClasses.Title}>RomM Armada</div>,
    content: <Root />,
    icon: <Icon />,
    onDismount() {
      stop();
    },
  };
});
