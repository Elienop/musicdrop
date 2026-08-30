import { describe, expect, test } from "vitest";

// The TRACKED schema, as bytes, through Vite's `?raw` import rather than
// `node:fs`. Two reasons it is not read with fs: `tsconfig.app.json` carries no
// `"node"` types (it describes a browser bundle, and adding them would let any
// app module import a Node builtin), and the jsdom environment gives modules an
// `http://localhost` base so `import.meta.url` is not a `file:` URL. `?raw`
// resolves at transform time against this file, so it needs no cwd either.
import specJson from "../../openapi.json?raw";

import { GATE_EXEMPT_PATHS } from "@/api/client";

/**
 * The client's exemption set against the CONTRACT's.
 *
 * `api/client.ts`'s `GATE_EXEMPT_PATHS` and
 * `backend/app/auth/gate.py::EXEMPT_PATHS` are hand-maintained in two
 * languages and nothing failed if they diverged — the client could go on
 * treating a path as exempt long after the server started gating it, and a 401
 * that means "your session is gone" would be swallowed instead of bouncing
 * anyone to /login.
 *
 * The tracked `frontend/openapi.json` already carries the gate's own answer,
 * and not by convention: `backend/app/openapi_overlay.py` asks
 * `path_requires_session` — the SAME predicate the middleware runs — and
 * writes `security: []` onto every operation it exempts (that is OpenAPI's way
 * of cancelling the document-wide requirement for one operation), while every
 * gated operation instead gets a 401 response. So the spec is a real witness
 * to the server's behaviour rather than a second hand-written list.
 *
 * Read off disk rather than imported, so this asks about the file that is
 * COMMITTED and CI-guarded, and fails the same way whether the schema was
 * regenerated and the client forgotten or the other way round.
 *
 */
const spec = JSON.parse(specJson) as {
  paths: Record<string, Record<string, { security?: unknown[] } | unknown>>;
};

const HTTP_METHODS = new Set([
  "get",
  "put",
  "post",
  "delete",
  "options",
  "head",
  "patch",
  "trace",
]);

/** Every path with at least one operation the overlay marked gate-EXEMPT. */
function specExemptPaths(): Set<string> {
  const exempt = new Set<string>();
  for (const [path, item] of Object.entries(spec.paths)) {
    for (const [method, operation] of Object.entries(item)) {
      if (!HTTP_METHODS.has(method)) continue;
      const security = (operation as { security?: unknown }).security;
      if (Array.isArray(security) && security.length === 0) {
        exempt.add(path);
      }
    }
  }
  return exempt;
}

/**
 * The two paths the client deliberately leaves OUT of its own set, quoted from
 * the reasoning in `api/client.ts`: both are exempt server-side, so neither can
 * answer 401 at all, and a member no test could distinguish from its absence
 * would make the set look better covered than it is.
 *
 * Named here rather than tolerated as "any difference", because the direction
 * that matters is a NEW server exemption appearing and nobody noticing.
 */
const DELIBERATELY_ABSENT = ["/api/health", "/api/slskd/webhook"].sort();

describe("the client's gate exemptions against the tracked OpenAPI contract", () => {
  test("the spec really does encode the gate's decision", () => {
    // Non-vacuity first. Every assertion below is about a set derived from
    // `security: []`, and all of them would pass trivially against a spec that
    // had stopped emitting it — an empty set is a subset of everything.
    const exempt = specExemptPaths();
    expect(exempt.size).toBeGreaterThan(0);
    const gated = Object.keys(spec.paths).filter((p) => !exempt.has(p));
    expect(gated.length).toBeGreaterThan(0);
    // The one the client's comment calls out by name: /api/auth/logout sits in
    // the auth family but is GATED, which is why exempting it once deadlocked
    // sign-out.
    expect(exempt.has("/api/auth/logout")).toBe(false);
  });

  test("every path the client exempts is one the server exempts", () => {
    // The dangerous direction. If the backend starts gating a path this set
    // still names, its 401 means "your session is gone" and the middleware
    // returns without flipping the store — the user sits in a shell whose
    // every request is being refused, with nothing saying so.
    const exempt = specExemptPaths();
    for (const path of GATE_EXEMPT_PATHS) {
      expect(exempt).toContain(path);
    }
  });

  test("the paths it leaves out are exactly the two it means to leave out", () => {
    // The other direction, pinned rather than waved through: a THIRD exempt
    // path appearing in the contract has to be a decision someone makes here,
    // not a silence. Equally, un-exempting /api/health server-side without
    // touching this file fails.
    const exempt = specExemptPaths();
    const missing = [...exempt].filter((p) => !GATE_EXEMPT_PATHS.has(p)).sort();
    expect(missing).toEqual(DELIBERATELY_ABSENT);
  });
});
