// A stand-in for @decky/api in the UI tests: calls go to a scriptable fake backend, events are fired by hand.
type Listener = (...a: any[]) => any;

export const backend: {
  handlers: Record<string, (...a: any[]) => any>;
  calls: { method: string; args: any[] }[];
} = { handlers: {}, calls: [] };

const listeners = new Map<string, Set<Listener>>();

export const call = async (route: string, ...args: any[]) => {
  backend.calls.push({ method: route, args });
  const h = backend.handlers[route];
  if (!h) throw new Error(`the test gave no handler for "${route}"`);
  return await h(...args);
};

export const callable = (route: string) => (...args: any[]) => call(route, ...args);

export const addEventListener = (event: string, listener: Listener) => {
  if (!listeners.has(event)) listeners.set(event, new Set());
  listeners.get(event)!.add(listener);
  return listener;
};

export const removeEventListener = (event: string, listener: Listener) => {
  listeners.get(event)?.delete(listener);
};

export const fire = (event: string, ...args: any[]) => listeners.get(event)?.forEach((l) => l(...args));
export const listenerCount = () => [...listeners.values()].reduce((n, s) => n + s.size, 0);

export const toaster = {
  toasts: [] as any[],
  toast(t: any) {
    this.toasts.push(t);
    return { dismiss() {} };
  },
};

export enum FileSelectionType {
  FILE,
  FOLDER,
}

export const picker = { next: { path: "/run/media/mmcblk0p1/ROMs", realpath: "/run/media/mmcblk0p1/ROMs" } as any };
export const openFilePicker = async (..._args: any[]) => picker.next;

export const definePlugin = (fn: any) => fn;
export const routerHook = {};
export const reset = () => {
  backend.handlers = {};
  backend.calls = [];
  listeners.clear();
  toaster.toasts = [];
};
