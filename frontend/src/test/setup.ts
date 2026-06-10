import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterAll, afterEach, beforeAll, vi } from "vitest";

import { server } from "./msw-server";

// jsdom has no layout engine, so window.scrollTo is unimplemented and throws a
// "Not implemented" error when components call it (e.g. scroll-to-top on page
// change). Stub it so those code paths run cleanly under tests.
vi.stubGlobal("scrollTo", vi.fn());

// Node >=22 defines an experimental `localStorage` getter on globalThis that
// returns undefined unless --localstorage-file is set; under vitest's
// populateGlobal it shadows jsdom's real localStorage. Replace it with an
// in-memory Storage so components can persist (e.g. the sidebar collapse).
if (globalThis.localStorage === undefined) {
  const store = new Map<string, string>();
  const memoryStorage: Storage = {
    get length() {
      return store.size;
    },
    clear: () => {
      store.clear();
    },
    getItem: (key: string) => store.get(key) ?? null,
    key: (index: number) => [...store.keys()][index] ?? null,
    removeItem: (key: string) => {
      store.delete(key);
    },
    setItem: (key: string, value: string) => {
      store.set(key, String(value));
    },
  };
  Object.defineProperty(globalThis, "localStorage", {
    value: memoryStorage,
    writable: true,
    configurable: true,
  });
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  cleanup();
  server.resetHandlers();
});
afterAll(() => server.close());
