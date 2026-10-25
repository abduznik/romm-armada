// jsdom first: react-dom wants a window when it loads.
import { JSDOM } from "jsdom";

const dom = new JSDOM("<!doctype html><html><body><div id=root></div></body></html>", { url: "http://localhost/", pretendToBeVisual: true });
const g = globalThis as any;
for (const [k, v] of Object.entries({
  window: dom.window, document: dom.window.document, navigator: dom.window.navigator, HTMLElement: dom.window.HTMLElement,
  HTMLInputElement: dom.window.HTMLInputElement, Event: dom.window.Event, MouseEvent: dom.window.MouseEvent,
  IS_REACT_ACT_ENVIRONMENT: true,
})) {
  Object.defineProperty(g, k, { value: v, configurable: true, writable: true });   // node's own navigator has only a getter
}

const React = await import("react");
const { createRoot } = await import("react-dom/client");
const act: (fn: () => any) => Promise<void> = (React as any).act;

export type Handle = { root: ReturnType<typeof createRoot>; el: HTMLElement; unmount: () => Promise<void> };

const mounted: Handle[] = [];

export async function mount(node: any): Promise<Handle> {
  const el = document.createElement("div");
  document.body.appendChild(el);
  const root = createRoot(el);
  await act(async () => root.render(node));
  const handle: Handle = {
    root, el,
    unmount: async () => {
      const i = mounted.indexOf(handle);
      if (i >= 0) mounted.splice(i, 1);
      await act(async () => root.unmount());
      el.remove();
    },
  };
  mounted.push(handle);
  return handle;
}

/** Unmounts whatever a test left on the screen, so a failed test cannot disturb the next one. */
export async function cleanup(): Promise<void> {
  for (const h of [...mounted]) await h.unmount();
}

/** Lets pending promises and effects settle. */
export async function settle(times = 3, ms = 0): Promise<void> {
  for (let i = 0; i < times; i++) {
    await act(async () => {
      await new Promise((r) => setTimeout(r, ms));
    });
  }
}

export const text = (h: Handle) => h.el.textContent ?? "";

export function buttons(h: Handle): HTMLButtonElement[] {
  return [...h.el.querySelectorAll("button")] as HTMLButtonElement[];
}

export function findButton(h: Handle, label: string | RegExp): HTMLButtonElement {
  const hit = buttons(h).find((b) => (typeof label === "string" ? (b.textContent ?? "").includes(label) : label.test(b.textContent ?? "")));
  if (!hit) throw new Error(`no button "${label}"; the screen has: ${buttons(h).map((b) => JSON.stringify(b.textContent)).join(", ")}`);
  return hit;
}

export async function click(h: Handle, label: string | RegExp, settleTimes = 3): Promise<void> {
  const b = findButton(h, label);
  await act(async () => {
    b.dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true }));
  });
  await settle(settleTimes);
}

export function input(h: Handle, label: string): HTMLInputElement {
  const hit = h.el.querySelector(`input[aria-label="${label}"]`) as HTMLInputElement | null;
  if (!hit) throw new Error(`no input "${label}"`);
  return hit;
}

export function select(h: Handle, label: string): HTMLSelectElement {
  const hit = h.el.querySelector(`select[aria-label="${label}"]`) as HTMLSelectElement | null;
  if (!hit) throw new Error(`no select "${label}"`);
  return hit;
}

/** Types into a React-controlled input the way a keyboard would. */
export async function type(h: Handle, label: string, value: string): Promise<void> {
  const el = input(h, label);
  const setter = Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, "value")!.set!;
  await act(async () => {
    setter.call(el, value);
    el.dispatchEvent(new dom.window.Event("input", { bubbles: true }));
  });
  await settle(1);
}

export async function toggle(h: Handle, label: string): Promise<void> {
  const el = input(h, label);
  await act(async () => {
    el.dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true }));
  });
  await settle(3);
}

export async function choose(h: Handle, label: string, value: string): Promise<void> {
  const el = select(h, label);
  const setter = Object.getOwnPropertyDescriptor(dom.window.HTMLSelectElement.prototype, "value")!.set!;
  await act(async () => {
    setter.call(el, value);
    el.dispatchEvent(new dom.window.Event("change", { bubbles: true }));
  });
  await settle(3);
}

export async function slide(h: Handle, label: string, value: number): Promise<void> {
  const el = input(h, label);
  const setter = Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, "value")!.set!;
  await act(async () => {
    setter.call(el, String(value));
    el.dispatchEvent(new dom.window.Event("input", { bubbles: true }));
  });
  await settle(3);
}

export { act, React };
