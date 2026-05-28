/**
 * SettingsPage flows for the L3 editor (T13).
 *
 * Covers the five-state machine (clean / dirty / saving / apply_pending /
 * applying) the page derives from React Query + a local `dirty` flag, the
 * Mod-s keymap registered by the CM6 extension list, the linter's 422 ->
 * gutter pipeline, and the 409 conflict modal (with both Reload and Overwrite
 * exits). The page renders CodeMirror so DOM queries pin on the editor's
 * `.cm-content` contenteditable rather than the obsolete read-only `<pre>`
 * the L1/L2 tests used.
 */
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterAll, beforeAll, describe, expect, test } from "vitest";

import type { components } from "@/api/schema";
import { SettingsPage } from "@/pages/settings/SettingsPage";
import { server } from "@/test/msw-server";
import { renderWithProviders } from "@/test/render";

// CodeMirror calls `range.getClientRects()` from its rAF-driven measure pass,
// but jsdom's `Range` doesn't implement it at all (jsdom #3032). Without a
// stub, the measure pass — which gets scheduled BY the editor's mount and
// fires AFTER the test's afterEach cleanup tears down the DOM — throws an
// uncaught TypeError that fails the file's exit code even though every test
// `expect` passed. Patching `Range.prototype` here covers every test in the
// suite without touching the shared global `test/setup.ts`. We hold the
// originals so the file's `afterAll` can restore them and leave jsdom in the
// state the next test file expects.
//
// `Range` declares these methods non-optional in the TS DOM lib, so the
// erase-on-restore uses `Object.defineProperty(..., undefined)` rather than
// `delete` to satisfy `--strict`'s "operand of delete must be optional" rule.
const RANGE_PROTO = Range.prototype;
const ORIGINAL_GET_CLIENT_RECTS = RANGE_PROTO.getClientRects;
const ORIGINAL_GET_BOUNDING = RANGE_PROTO.getBoundingClientRect;

beforeAll(() => {
  // `DOMRectList` is iterable + has `.length`; an empty array literal is
  // assignment-compatible enough for CM6's clientRectsFor (which calls
  // `Array.from(...)` on the result).
  RANGE_PROTO.getClientRects = function getClientRects() {
    return [] as unknown as DOMRectList;
  };
  RANGE_PROTO.getBoundingClientRect = function getBoundingClientRect() {
    return {
      x: 0,
      y: 0,
      width: 0,
      height: 0,
      top: 0,
      left: 0,
      right: 0,
      bottom: 0,
    } as DOMRect;
  };
});

afterAll(() => {
  RANGE_PROTO.getClientRects = ORIGINAL_GET_CLIENT_RECTS;
  RANGE_PROTO.getBoundingClientRect = ORIGINAL_GET_BOUNDING;
});

type BeetsConfigSnapshot = components["schemas"]["BeetsConfigSnapshot"];
type SaveRequest = components["schemas"]["SaveRequest"];

const CONFIG_URL = `${window.location.origin}/api/config`;
const SAVE_URL = `${window.location.origin}/api/config/save`;
const APPLY_URL = `${window.location.origin}/api/config/apply`;
const VALIDATE_URL = `${window.location.origin}/api/config/validate`;
const ACTIVE_IMPORT_URL = `${window.location.origin}/api/imports/active`;

const SAMPLE_YAML =
  "directory: /music\nlibrary: library.db\nplugins:\n  - musicbrainz\n  - deezer\n";

function snapshotFixture(
  overrides: Partial<BeetsConfigSnapshot> = {},
): BeetsConfigSnapshot {
  return {
    yaml_text: SAMPLE_YAML,
    config_path: "/abs/data/beets/config.yaml",
    loaded_at: "2026-05-28T14:23:00Z",
    file_modified_at: "2026-05-28T14:23:00Z",
    mtime_ns: 1,
    sha256: "base-sha",
    apply_pending: false,
    ...overrides,
  };
}

/**
 * Register the always-needed default mocks for routes the page polls or hits
 * but a given test doesn't care about. Keeps each test's `server.use` focused
 * on the route(s) under test rather than re-registering the probe + validate
 * stubs everywhere. Tests can still override by calling `server.use(...)`
 * AFTER calling this helper — MSW resolves last-registered-first.
 */
function defaultMocks(
  snapshot: BeetsConfigSnapshot = snapshotFixture(),
  importActive = false,
) {
  server.use(
    http.get(CONFIG_URL, () => HttpResponse.json(snapshot)),
    http.get(ACTIVE_IMPORT_URL, () =>
      HttpResponse.json({ active: importActive }),
    ),
    http.post(VALIDATE_URL, () => HttpResponse.json({ errors: [] })),
  );
}

/**
 * Pull CodeMirror's contenteditable content element after the editor mounts.
 * The page's `<CodeMirror>` instance renders a `.cm-content` div as its only
 * editing surface, so anchoring on that class is the stable cross-render
 * handle. Wrapping in `findBy*`-style `waitFor` lets the mount race with the
 * MSW data fetch without flake.
 */
async function findEditorContent(): Promise<HTMLElement> {
  return await waitFor(() => {
    const el = document.querySelector(".cm-content");
    if (!el) throw new Error(".cm-content not mounted yet");
    return el as HTMLElement;
  });
}

function renderPage() {
  return renderWithProviders(<SettingsPage />, {
    route: "/settings",
    path: "/settings",
  });
}

describe("SettingsPage", () => {
  test("renders loading state while the snapshot is fetching", async () => {
    // Never-resolving handler so the page is wedged in `isPending`. `HttpResponse`
    // is generic over the body shape — pin a concrete BeetsConfigSnapshot so the
    // Promise's resolve type stays well-formed even though we never call it.
    server.use(
      http.get(
        CONFIG_URL,
        () => new Promise<HttpResponse<BeetsConfigSnapshot>>(() => {}),
      ),
    );
    renderPage();
    // The Loader's text is the stable marker (role="status" carries it for a11y).
    expect(
      await screen.findByText(/loading configuration/i),
    ).toBeInTheDocument();
  });

  test("renders error state when the snapshot fetch fails", async () => {
    server.use(
      http.get(CONFIG_URL, () =>
        HttpResponse.json({ detail: "boom" }, { status: 500 }),
      ),
    );
    renderPage();
    expect(
      await screen.findByText(/could not load configuration/i),
    ).toBeInTheDocument();
  });

  test("renders the YAML inside CodeMirror after the snapshot loads", async () => {
    defaultMocks();
    renderPage();
    // Heading paints immediately; the path line only appears once the data
    // arrives. Pin on the path string for the "data is here" signal.
    expect(
      await screen.findByText(/data\/beets\/config\.yaml/),
    ).toBeInTheDocument();
    const content = await findEditorContent();
    // CodeMirror chunks the doc into per-line `<div class="cm-line">`s, so
    // assert against text content rather than a single text node.
    expect(content.textContent).toContain("directory:");
    expect(content.textContent).toContain("plugins:");
    expect(content.textContent).toContain("musicbrainz");
  });

  test("clicking Edit flips the editor out of read-only", async () => {
    defaultMocks();
    const user = userEvent.setup();
    renderPage();
    await findEditorContent();

    // Initial render: read-only triplet sets contenteditable=false.
    const contentBefore = (await findEditorContent()).getAttribute(
      "contenteditable",
    );
    expect(contentBefore).toBe("false");

    await user.click(screen.getByRole("button", { name: /^edit$/i }));

    // After Edit: the compartment reconfigures to [] -> contenteditable=true.
    await waitFor(() => {
      const after = document
        .querySelector(".cm-content")
        ?.getAttribute("contenteditable");
      expect(after).toBe("true");
    });
  });

  test("typing transitions the page to dirty state", async () => {
    defaultMocks();
    const user = userEvent.setup();
    renderPage();
    const content = await findEditorContent();

    await user.click(screen.getByRole("button", { name: /^edit$/i }));
    // userEvent.type needs the editor focused; clicking it doesn't always
    // land focus on the contenteditable child (CM6 owns the focus chain), so
    // call focus + type a single char.
    content.focus();
    await user.keyboard("x");

    // The "Unsaved changes" banner is the page's primary dirty signal.
    expect(
      await screen.findByText(/unsaved changes/i),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^save$/i })).toBeEnabled();
  });

  test("Mod-s posts a Save with the snapshot's CAS tokens", async () => {
    let seenBody: SaveRequest | null = null;
    defaultMocks();
    server.use(
      http.post(SAVE_URL, async ({ request }) => {
        seenBody = (await request.json()) as SaveRequest;
        return HttpResponse.json(
          snapshotFixture({ mtime_ns: 2, sha256: "fresh-sha", apply_pending: true }),
        );
      }),
    );
    const user = userEvent.setup();
    renderPage();
    const content = await findEditorContent();

    await user.click(screen.getByRole("button", { name: /^edit$/i }));
    content.focus();
    await user.keyboard("z");
    // CM6 registers `Mod-s` via `Prec.high(keymap.of([...]))` so it fires
    // before any browser shortcut. userEvent v14's modifier syntax is `{Mod>}s{/Mod}`,
    // which maps to Meta on Mac and Control elsewhere.
    await user.keyboard("{Control>}s{/Control}");

    await waitFor(() => expect(seenBody).not.toBeNull());
    expect(seenBody).not.toBeNull();
    // Body should carry the CAS tokens taken from the initial snapshot,
    // and yaml_text should include the typed character.
    expect(seenBody!.base_mtime_ns).toBe(1);
    expect(seenBody!.base_sha256).toBe("base-sha");
    expect(seenBody!.yaml_text).toContain("directory:");
    expect(seenBody!.yaml_text.length).toBeGreaterThan(SAMPLE_YAML.length);
  });

  test("save success surfaces the apply_pending banner and enables Apply", async () => {
    // First GET = clean snapshot; the POST returns apply_pending=true; the
    // invalidate-triggered refetch gets the same apply_pending=true so the
    // post-Save resting state shows "saved on disk but not loaded".
    let getCalls = 0;
    server.use(
      http.get(CONFIG_URL, () => {
        getCalls += 1;
        return HttpResponse.json(
          snapshotFixture({
            apply_pending: getCalls > 1,
            mtime_ns: getCalls,
            sha256: `sha-${getCalls}`,
          }),
        );
      }),
      http.get(ACTIVE_IMPORT_URL, () =>
        HttpResponse.json({ active: false }),
      ),
      http.post(VALIDATE_URL, () => HttpResponse.json({ errors: [] })),
      http.post(SAVE_URL, () =>
        HttpResponse.json(
          snapshotFixture({ apply_pending: true, mtime_ns: 2, sha256: "sha-2" }),
        ),
      ),
    );
    const user = userEvent.setup();
    renderPage();
    const content = await findEditorContent();

    await user.click(screen.getByRole("button", { name: /^edit$/i }));
    content.focus();
    await user.keyboard("y");
    await user.click(screen.getByRole("button", { name: /^save$/i }));

    // The yellow apply_pending banner is the resting state; "click Apply" is
    // its distinguishing copy (separate from "Apply available once the
    // running import finishes" — see the importActive branch below).
    expect(
      await screen.findByText(/click apply to load it into beets/i),
    ).toBeInTheDocument();
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: /apply changes/i }),
      ).toBeEnabled(),
    );
  });

  test("apply success returns the page to clean (Edit re-enabled, Apply disabled)", async () => {
    // GETs alternate between apply_pending=true (initial) and false (post-apply).
    let getCalls = 0;
    server.use(
      http.get(CONFIG_URL, () => {
        getCalls += 1;
        return HttpResponse.json(
          snapshotFixture({
            apply_pending: getCalls === 1,
            mtime_ns: getCalls,
            sha256: `sha-${getCalls}`,
          }),
        );
      }),
      http.get(ACTIVE_IMPORT_URL, () =>
        HttpResponse.json({ active: false }),
      ),
      http.post(VALIDATE_URL, () => HttpResponse.json({ errors: [] })),
      http.post(APPLY_URL, () =>
        HttpResponse.json(snapshotFixture({ apply_pending: false, mtime_ns: 2 })),
      ),
    );
    const user = userEvent.setup();
    renderPage();
    await findEditorContent();

    // Wait for apply_pending state (Apply enabled, Edit disabled).
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: /apply changes/i }),
      ).toBeEnabled(),
    );
    expect(screen.getByRole("button", { name: /^edit$/i })).toBeDisabled();

    await user.click(screen.getByRole("button", { name: /apply changes/i }));

    // Post-apply: state returns to clean. Edit becomes enabled (clean is the
    // only state where Edit is clickable) and Apply turns off.
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /^edit$/i })).toBeEnabled(),
    );
    expect(
      screen.getByRole("button", { name: /apply changes/i }),
    ).toBeDisabled();
  });

  test("Apply is disabled while an import is active and shows the helper text", async () => {
    let getCalls = 0;
    server.use(
      http.get(CONFIG_URL, () => {
        getCalls += 1;
        // apply_pending=true from the start so Apply would normally be on.
        return HttpResponse.json(
          snapshotFixture({
            apply_pending: true,
            mtime_ns: getCalls,
            sha256: `sha-${getCalls}`,
          }),
        );
      }),
      http.get(ACTIVE_IMPORT_URL, () =>
        HttpResponse.json({ active: true }),
      ),
      http.post(VALIDATE_URL, () => HttpResponse.json({ errors: [] })),
    );
    renderPage();
    await findEditorContent();

    // The button must stay disabled (importActive gate) and the inline
    // helper text must mention the running import.
    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: /apply changes/i }),
      ).toBeDisabled();
    });
    expect(
      await screen.findByText(/1 import running/i),
    ).toBeInTheDocument();
  });

  test("a 422 from validate paints an error marker in the CodeMirror gutter", async () => {
    defaultMocks();
    // Override validate so a typed character produces a single error row.
    server.use(
      http.post(VALIDATE_URL, () =>
        HttpResponse.json({
          errors: [
            {
              loc: "import.copy",
              msg: "must be a boolean",
              type: "schema_type",
              line: 1,
              column: 0,
            },
          ],
        }),
      ),
    );
    const user = userEvent.setup();
    renderPage();
    const content = await findEditorContent();

    await user.click(screen.getByRole("button", { name: /^edit$/i }));
    content.focus();
    // Type a character to dirty the buffer and trigger CM6's linter, which
    // debounces at 500ms (see codemirror-config.ts).
    await user.keyboard("x");

    // The linter's debounce is 500ms. We wait long enough (4s ceiling) for
    // the gutter marker to appear, but otherwise leave the clock alone —
    // CM6's linter uses real timers internally and faking them tends to
    // wedge the debounce promise chain.
    await waitFor(
      () => {
        const marker = document.querySelector(".cm-lint-marker-error");
        expect(marker).not.toBeNull();
      },
      { timeout: 4000 },
    );
  });

  test("a 409 from save opens the conflict modal with Reload + Overwrite buttons", async () => {
    defaultMocks();
    server.use(
      http.post(SAVE_URL, () =>
        HttpResponse.json(
          {
            detail: {
              current_yaml_text: "directory: /music-fresh\n",
              current_sha256: "fresh-server-sha",
              current_snapshot: { mtime_ns: 999 },
            },
          },
          { status: 409 },
        ),
      ),
    );
    const user = userEvent.setup();
    renderPage();
    const content = await findEditorContent();

    await user.click(screen.getByRole("button", { name: /^edit$/i }));
    content.focus();
    await user.keyboard("q");
    await user.click(screen.getByRole("button", { name: /^save$/i }));

    // The conflict modal is a role="dialog" with a stable aria-label.
    const modal = await screen.findByRole("dialog", {
      name: /file changed on disk/i,
    });
    expect(modal).toBeInTheDocument();
    expect(
      within(modal).getByRole("button", { name: /reload \(drop my edits\)/i }),
    ).toBeInTheDocument();
    expect(
      within(modal).getByRole("button", { name: /overwrite anyway/i }),
    ).toBeInTheDocument();
  });

  test("Reload in the conflict modal closes the modal and returns to clean", async () => {
    defaultMocks();
    server.use(
      http.post(SAVE_URL, () =>
        HttpResponse.json(
          {
            detail: {
              current_yaml_text: "directory: /music-fresh\n",
              current_sha256: "fresh-server-sha",
              current_snapshot: { mtime_ns: 999 },
            },
          },
          { status: 409 },
        ),
      ),
    );
    const user = userEvent.setup();
    renderPage();
    const content = await findEditorContent();

    await user.click(screen.getByRole("button", { name: /^edit$/i }));
    content.focus();
    await user.keyboard("q");
    await user.click(screen.getByRole("button", { name: /^save$/i }));

    const modal = await screen.findByRole("dialog", {
      name: /file changed on disk/i,
    });
    await user.click(
      within(modal).getByRole("button", { name: /reload \(drop my edits\)/i }),
    );

    await waitFor(() =>
      expect(
        screen.queryByRole("dialog", { name: /file changed on disk/i }),
      ).not.toBeInTheDocument(),
    );
    // Clean state: Edit is enabled again, the dirty banner is gone.
    expect(screen.getByRole("button", { name: /^edit$/i })).toBeEnabled();
    expect(screen.queryByText(/unsaved changes/i)).not.toBeInTheDocument();
  });

  test("Overwrite anyway re-Saves with the conflict body's fresh CAS tokens", async () => {
    defaultMocks();
    const savedBodies: SaveRequest[] = [];
    server.use(
      http.post(SAVE_URL, async ({ request }) => {
        const body = (await request.json()) as SaveRequest;
        savedBodies.push(body);
        if (savedBodies.length === 1) {
          // First Save: 409 with fresh tokens.
          return HttpResponse.json(
            {
              detail: {
                current_yaml_text: "directory: /music-fresh\n",
                current_sha256: "fresh-server-sha",
                current_snapshot: { mtime_ns: 999 },
              },
            },
            { status: 409 },
          );
        }
        // Second Save (Overwrite): 200 with the new snapshot.
        return HttpResponse.json(
          snapshotFixture({ apply_pending: true, mtime_ns: 1000 }),
        );
      }),
    );
    const user = userEvent.setup();
    renderPage();
    const content = await findEditorContent();

    await user.click(screen.getByRole("button", { name: /^edit$/i }));
    content.focus();
    await user.keyboard("q");
    await user.click(screen.getByRole("button", { name: /^save$/i }));

    const modal = await screen.findByRole("dialog", {
      name: /file changed on disk/i,
    });
    await user.click(
      within(modal).getByRole("button", { name: /overwrite anyway/i }),
    );

    await waitFor(() => expect(savedBodies).toHaveLength(2));
    // First Save used the initial snapshot's CAS tokens. The second one
    // must carry the conflict body's `current_*` tokens, NOT the original.
    expect(savedBodies[0].base_mtime_ns).toBe(1);
    expect(savedBodies[0].base_sha256).toBe("base-sha");
    expect(savedBodies[1].base_mtime_ns).toBe(999);
    expect(savedBodies[1].base_sha256).toBe("fresh-server-sha");
  });
});

