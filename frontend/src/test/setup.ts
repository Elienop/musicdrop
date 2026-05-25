import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterAll, afterEach, beforeAll, vi } from "vitest";

import { server } from "./msw-server";

// jsdom has no layout engine, so window.scrollTo is unimplemented and throws a
// "Not implemented" error when components call it (e.g. scroll-to-top on page
// change). Stub it so those code paths run cleanly under tests.
vi.stubGlobal("scrollTo", vi.fn());

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  cleanup();
  server.resetHandlers();
});
afterAll(() => server.close());
