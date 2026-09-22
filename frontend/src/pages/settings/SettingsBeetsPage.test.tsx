/**
 * SettingsBeetsPage flows for the L3 editor (T13 → Phase-3 split).
 *
 * Covers the five-state machine (clean / dirty / saving / apply_pending /
 * applying) the page derives from React Query + a local `dirty` flag, the
 * Mod-s keymap registered by the CM6 extension list, the linter's 422 ->
 * gutter pipeline, and the 409 conflict modal (with both Reload and Overwrite
 * exits). The page renders CodeMirror so DOM queries pin on the editor's
 * `.cm-content` contenteditable rather than the obsolete read-only `<pre>`
 * the L1/L2 tests used.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { createMemoryRouter, Navigate, RouterProvider } from "react-router";
import {
  afterAll,
  beforeAll,
  beforeEach,
  describe,
  expect,
  test,
} from "vitest";

import type { components } from "@/api/schema";
import { SettingsBeetsPage } from "@/pages/settings/SettingsBeetsPage";
import { SettingsLayout } from "@/pages/settings/SettingsLayout";
import { server } from "@/test/msw-server";

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
const REORGANIZE_STATUS_URL = `${window.location.origin}/api/reorganize/status`;
const DISK_SYNC_STATUS_URL = `${window.location.origin}/api/disk-sync/status`;

/** Idle reorganize job — shape mirrors useReorganizeStatus's fallback. */
function idleReorganizeStatus() {
  return {
    phase: "idle",
    job_id: null,
    scope: null,
    total: 0,
    processed: 0,
    moved: 0,
    skipped: 0,
    failed: 0,
    current: null,
    error: null,
    artist: null,
    album_id: null,
    scope_label: "library",
  };
}

/** Idle disk-sync job — shape mirrors useDiskSyncStatus's fallback. The page
 * now hosts the DiskSyncPanel, which polls this probe on mount. */
function idleDiskSyncStatus() {
  return {
    phase: "idle",
    job_id: null,
    total: 0,
    processed: 0,
    removed: 0,
    updated: 0,
    unchanged: 0,
    read_errors: 0,
    emptied_albums: 0,
    current: null,
    error: null,
    failures: [],
  };
}

beforeEach(() => {
  server.use(
    http.get(REORGANIZE_STATUS_URL, () =>
      HttpResponse.json(idleReorganizeStatus()),
    ),
    http.get(DISK_SYNC_STATUS_URL, () =>
      HttpResponse.json(idleDiskSyncStatus()),
    ),
  );
});

const SAMPLE_YAML =
  "directory: /music\nlibrary: library.db\nplugins:\n  - musicbrainz\n  - deezer\n";

// The fully-merged effective config the backend renders (beets + plugin
// defaults, secrets redacted). Distinct from SAMPLE_YAML so tests can assert
// the read-only panel surfaces THIS document, not the editable raw file.
const EFFECTIVE_YAML =
  "directory: /music\nlibrary: library.db\nimport:\n  copy: true\n  write: true\nplugins:\n  - musicbrainz\n  - deezer\n";

function snapshotFixture(
  overrides: Partial<BeetsConfigSnapshot> = {},
): BeetsConfigSnapshot {
  return {
    yaml_text: SAMPLE_YAML,
    effective_yaml: EFFECTIVE_YAML,
    config_path: "/abs/data/beets/config.yaml",
    loaded_at: "2026-05-28T14:23:00Z",
    file_modified_at: "2026-05-28T14:23:00Z",
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
    http.post(VALIDATE_URL, () =>
      HttpResponse.json({ errors: [], advisories: [] }),
    ),
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
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const router = createMemoryRouter(
    [
      {
        path: "/settings",
        element: <SettingsLayout />,
        children: [
          { index: true, element: <Navigate to="/settings/beets" replace /> },
          { path: "beets", element: <SettingsBeetsPage /> },
        ],
      },
    ],
    { initialEntries: ["/settings"] },
  );
  return render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
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

  test("surfaces the effective config in a read-only panel below the editor", async () => {
    defaultMocks();
    renderPage();
    // Wait for the editable editor to mount first (it's the first `.cm-content`).
    await findEditorContent();

    // The effective-config pane is its own labeled region so it's queryable
    // independent of the editable editor above it.
    const region = await screen.findByRole("region", {
      name: /effective config/i,
    });
    // It renders the MERGED effective_yaml — assert on a key that lives only in
    // EFFECTIVE_YAML (the plugin-default `write: true`), never in the editable
    // SAMPLE_YAML, so this can't accidentally match the raw editor's doc.
    await waitFor(() => {
      expect(region.textContent).toContain("write: true");
    });

    // The pane is read-only: its contenteditable surface reports false and
    // there's no Edit/Save affordance inside the region (it's never wired to
    // the save flow).
    const paneContent = region.querySelector(".cm-content");
    expect(paneContent?.getAttribute("contenteditable")).toBe("false");
    expect(within(region).queryByRole("button")).not.toBeInTheDocument();
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
    expect(await screen.findByText(/unsaved changes/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^save$/i })).toBeEnabled();
  });

  test("Mod-s posts a Save with the snapshot's CAS token", async () => {
    let seenBody: SaveRequest | null = null;
    defaultMocks();
    server.use(
      http.post(SAVE_URL, async ({ request }) => {
        seenBody = (await request.json()) as SaveRequest;
        return HttpResponse.json(
          snapshotFixture({ sha256: "fresh-sha", apply_pending: true }),
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
    // Body should carry the CAS token taken from the initial snapshot,
    // and yaml_text should include the typed character.
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
            sha256: `sha-${getCalls}`,
          }),
        );
      }),
      http.get(ACTIVE_IMPORT_URL, () => HttpResponse.json({ active: false })),
      http.post(VALIDATE_URL, () =>
      HttpResponse.json({ errors: [], advisories: [] }),
    ),
      http.post(SAVE_URL, () =>
        HttpResponse.json(
          snapshotFixture({ apply_pending: true, sha256: "sha-2" }),
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
            sha256: `sha-${getCalls}`,
          }),
        );
      }),
      http.get(ACTIVE_IMPORT_URL, () => HttpResponse.json({ active: false })),
      http.post(VALIDATE_URL, () =>
      HttpResponse.json({ errors: [], advisories: [] }),
    ),
      http.post(APPLY_URL, () =>
        HttpResponse.json(snapshotFixture({ apply_pending: false })),
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

  test("an Apply failure surfaces a destructive banner with the recovery hint", async () => {
    let getCalls = 0;
    server.use(
      http.get(CONFIG_URL, () => {
        getCalls += 1;
        return HttpResponse.json(
          snapshotFixture({ apply_pending: true, sha256: `sha-${getCalls}` }),
        );
      }),
      http.get(ACTIVE_IMPORT_URL, () => HttpResponse.json({ active: false })),
      http.post(VALIDATE_URL, () =>
      HttpResponse.json({ errors: [], advisories: [] }),
    ),
      http.post(APPLY_URL, () =>
        HttpResponse.json(
          {
            detail: {
              message: "Apply failed during rebuild: boom",
              recovery: "Apply stopped partway: boom. Fix that and Apply again.",
            },
          },
          { status: 500 },
        ),
      ),
    );
    const user = userEvent.setup();
    renderPage();
    await findEditorContent();
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: /apply changes/i }),
      ).toBeEnabled(),
    );

    await user.click(screen.getByRole("button", { name: /apply changes/i }));

    // The failure is surfaced (not silently swallowed back to the resting
    // banner), and the 500's recovery hint is shown inline.
    const banner = await screen.findByText(/apply failed/i);
    expect(banner).toHaveAttribute("role", "alert");
    expect(banner).toHaveTextContent(/apply stopped partway/i);
  });

  test("an Apply refused for a bad Trash layout shows the reason, not the job sentence", async () => {
    // The backend answers 422 (not 409) precisely so this branch runs: every
    // Apply 409 renders the fixed "A library job is running" line, which would
    // tell the operator the opposite of what happened. Pinned on the STATUS the
    // containment refusal uses, so a later `status === 422` branch that showed
    // generic copy would fail here.
    let getCalls = 0;
    server.use(
      http.get(CONFIG_URL, () => {
        getCalls += 1;
        return HttpResponse.json(
          snapshotFixture({ apply_pending: true, sha256: `sha-${getCalls}` }),
        );
      }),
      http.get(ACTIVE_IMPORT_URL, () => HttpResponse.json({ active: false })),
      http.post(VALIDATE_URL, () =>
        HttpResponse.json({ errors: [], advisories: [] }),
      ),
      http.post(APPLY_URL, () =>
        HttpResponse.json(
          {
            detail: {
              message: "Apply refused: The Trash directory is the music library",
              recovery:
                "The Trash directory is the music library — emptying it would" +
                " delete the music library. MUSICDROP_TRASH_DIR: '/music';" +
                " `directory:` in config.yaml: '/music'. Set MUSICDROP_TRASH_DIR" +
                " to its own folder.",
            },
          },
          { status: 422 },
        ),
      ),
    );
    const user = userEvent.setup();
    renderPage();
    await findEditorContent();
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: /apply changes/i }),
      ).toBeEnabled(),
    );

    await user.click(screen.getByRole("button", { name: /apply changes/i }));

    const banner = await screen.findByText(/apply failed/i);
    expect(banner).toHaveAttribute("role", "alert");
    expect(banner).toHaveTextContent(/MUSICDROP_TRASH_DIR/);
    expect(
      screen.queryByText(/a library job is running/i),
    ).not.toBeInTheDocument();
  });

  test("a non-409 Save failure surfaces a destructive banner", async () => {
    defaultMocks();
    server.use(
      http.post(SAVE_URL, () =>
        HttpResponse.json({ detail: "disk write failed" }, { status: 500 }),
      ),
    );
    const user = userEvent.setup();
    renderPage();
    const content = await findEditorContent();

    await user.click(screen.getByRole("button", { name: /^edit$/i }));
    content.focus();
    await user.keyboard("x");
    await user.click(screen.getByRole("button", { name: /^save$/i }));

    const banner = await screen.findByText(/save failed/i);
    expect(banner).toHaveAttribute("role", "alert");
  });

  test("Cancel after a failed Save clears the stale Save-failed alert", async () => {
    // A settled error mutation stays in its error state until reset, and the
    // alert is not gated on page state — so without a reset the red "Save
    // failed" banner would linger on an otherwise-clean page after Cancel.
    defaultMocks();
    server.use(
      http.post(SAVE_URL, () =>
        HttpResponse.json({ detail: "disk write failed" }, { status: 500 }),
      ),
    );
    const user = userEvent.setup();
    renderPage();
    const content = await findEditorContent();

    await user.click(screen.getByRole("button", { name: /^edit$/i }));
    content.focus();
    await user.keyboard("x");
    await user.click(screen.getByRole("button", { name: /^save$/i }));
    expect(await screen.findByText(/save failed/i)).toBeInTheDocument();

    // Discarding the edit returns the page to clean — the failure banner must go.
    await user.click(screen.getByRole("button", { name: /^cancel$/i }));
    await waitFor(() =>
      expect(screen.queryByText(/save failed/i)).not.toBeInTheDocument(),
    );
    expect(screen.getByRole("button", { name: /^edit$/i })).toBeEnabled();
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
            sha256: `sha-${getCalls}`,
          }),
        );
      }),
      http.get(ACTIVE_IMPORT_URL, () => HttpResponse.json({ active: true })),
      http.post(VALIDATE_URL, () =>
      HttpResponse.json({ errors: [], advisories: [] }),
    ),
    );
    renderPage();
    await findEditorContent();

    // The button must stay disabled (library-job gate) and the inline
    // helper text must mention the running import.
    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: /apply changes/i }),
      ).toBeDisabled();
    });
    expect(await screen.findByText(/import is running/i)).toBeInTheDocument();
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
          advisories: [],
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
    expect(modal).toHaveFocus();
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
    // Regression: the editor must visually drop the user's local edit AND
    // adopt the conflict body's `current_yaml_text` — otherwise the page
    // says "clean" while CM6's doc still holds the stale draft. The Reload
    // dispatch replaces the doc; assert the typed character ("q") is gone
    // and the fresh on-disk text is present.
    await waitFor(() => {
      const cm = document.querySelector(".cm-content")?.textContent ?? "";
      expect(cm).toContain("/music-fresh");
      expect(cm).not.toContain("qdirectory");
    });
    // The editor should also be back in read-only — the user dropped their
    // edits, so the next interaction must come from a fresh Edit click.
    expect(
      document.querySelector(".cm-content")?.getAttribute("contenteditable"),
    ).toBe("false");
  });

  test("Overwrite anyway re-Saves with the conflict body's fresh CAS tokens", async () => {
    defaultMocks();
    const savedBodies: SaveRequest[] = [];
    server.use(
      http.post(SAVE_URL, async ({ request }) => {
        const body = (await request.json()) as SaveRequest;
        savedBodies.push(body);
        if (savedBodies.length === 1) {
          // First Save: 409 with the fresh CAS token.
          return HttpResponse.json(
            {
              detail: {
                current_yaml_text: "directory: /music-fresh\n",
                current_sha256: "fresh-server-sha",
              },
            },
            { status: 409 },
          );
        }
        // Second Save (Overwrite): 200 with the new snapshot.
        return HttpResponse.json(
          snapshotFixture({
            apply_pending: true,
            sha256: "sha-after-overwrite",
          }),
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
    // First Save used the initial snapshot's CAS token. The second one
    // must carry the conflict body's `current_sha256`, NOT the original.
    expect(savedBodies[0].base_sha256).toBe("base-sha");
    expect(savedBodies[1].base_sha256).toBe("fresh-server-sha");
  });

  test("Save stays disabled while the linter reports validation errors", async () => {
    // Regression: previously the gutter marker was purely cosmetic — the user
    // could still click Save, the backend returned 422, and the failure was
    // silently swallowed by the mutation's onError. With this guard, lint
    // errors block the button until they clear.
    defaultMocks();
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
          advisories: [],
        }),
      ),
    );
    const user = userEvent.setup();
    renderPage();
    const content = await findEditorContent();

    await user.click(screen.getByRole("button", { name: /^edit$/i }));
    content.focus();
    await user.keyboard("x");

    // Wait for the debounced linter pass (CM6 fires at 500ms).
    await waitFor(
      () => {
        expect(document.querySelector(".cm-lint-marker-error")).not.toBeNull();
      },
      { timeout: 4000 },
    );

    expect(screen.getByRole("button", { name: /^save$/i })).toBeDisabled();
    // The inline helper text replaces the (mouse-only) tooltip for screen
    // readers, so assert it's present.
    expect(screen.getByText(/1 validation error/i)).toBeInTheDocument();
  });

  test("Mod-s in clean state does not fire Save (read-only guard)", async () => {
    // Regression: CM6's keymap fires Mod-s whenever the editor has focus,
    // including in read-only mode. Without the dirty-state guard a stray
    // Ctrl+S resaved the unchanged snapshot, advanced mtime, and lit up the
    // apply_pending banner — confusing for a user who didn't think they
    // edited anything.
    let saveCalls = 0;
    defaultMocks();
    server.use(
      http.post(SAVE_URL, () => {
        saveCalls += 1;
        return HttpResponse.json(snapshotFixture({ apply_pending: true }));
      }),
    );
    const user = userEvent.setup();
    renderPage();
    const content = await findEditorContent();

    // Focus the read-only editor and fire Ctrl-S WITHOUT clicking Edit.
    content.focus();
    await user.keyboard("{Control>}s{/Control}");

    // Give any in-flight mutation a tick to land, then assert it never
    // reached the network.
    await new Promise((r) => setTimeout(r, 50));
    expect(saveCalls).toBe(0);
  });

  test("Cancel after a 409 dismisses the conflict modal", async () => {
    // Regression for the bug where the diff stayed open after the user
    // backed out — handleCancel resets local edits but must also clear
    // the conflict state so the merge view unmounts.
    defaultMocks();
    server.use(
      http.post(SAVE_URL, () =>
        HttpResponse.json(
          {
            detail: {
              current_yaml_text: "directory: /music-fresh\n",
              current_sha256: "fresh-server-sha",
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
    await screen.findByRole("dialog", { name: /file changed on disk/i });

    await user.click(screen.getByRole("button", { name: /^cancel$/i }));

    await waitFor(() =>
      expect(
        screen.queryByRole("dialog", { name: /file changed on disk/i }),
      ).not.toBeInTheDocument(),
    );
    expect(screen.queryByText(/unsaved changes/i)).not.toBeInTheDocument();
  });

  test("/settings lands on the beets section inside the settings layout", async () => {
    defaultMocks();
    renderPage();
    expect(
      screen.getByRole("heading", { level: 1, name: "Settings" }),
    ).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByRole("link", { name: "Beets" })).toHaveAttribute(
        "aria-current",
        "page",
      ),
    );
    // The Loader's interim h2 carries the same accessible name and detaches
    // when the snapshot lands, so a findBy on the heading could resolve a
    // node the data swap removes. Wait on a loaded-only marker (the config
    // file path) instead, then assert the settled heading directly.
    await screen.findByText(/data\/beets\/config\.yaml/);
    expect(
      screen.getByRole("heading", { level: 2, name: "Beets configuration" }),
    ).toBeInTheDocument();
  });

  test("the beets section hosts the Reorganize panel", async () => {
    defaultMocks();
    renderPage();
    expect(
      await screen.findByRole("heading", {
        level: 2,
        name: "Reorganize library",
      }),
    ).toBeInTheDocument();
  });
});

/**
 * The ADVISORY channel of `POST /api/config/validate`.
 *
 * `ValidateResponse` carries two independent lists and the split is the whole
 * point: `errors` is what CodeMirror paints red, `advisories` is what it must
 * not. A config that only trips an advisory is VALID and saves cleanly, so
 * these tests pin the advisory surface as SEPARATE from the lint gutter — a
 * regression that merged the channels would make a correct config look broken.
 */
describe("SettingsBeetsPage config advisories", () => {
  // Two real rules from the backend's `_IMPORT_ADVISORY_RULES`, abridged. The
  // message is the backend's copy verbatim; the page never authors advisory
  // prose of its own, so the fixture text is what must appear on screen.
  const AUTOTAG_ADVISORY = {
    key: "import.autotag",
    message:
      "MusicDrop forces import.autotag on for every import it runs, so this value is discarded in the app.",
  };
  const SINGLETONS_ADVISORY = {
    key: "import.singletons",
    message:
      "MusicDrop forces import.singletons off on every import path it runs, review imports included.",
  };
  const COPY_LINT_ERROR = {
    loc: "import.copy",
    msg: "must be a boolean",
    type: "schema_type",
    line: 1,
    column: 0,
  };

  /**
   * Register a validate stub AND count its hits. The hit counter is what makes
   * the empty-advisories test non-vacuous: without it, "nothing rendered" would
   * pass just as well on a page that never validated anything at all.
   */
  function validateStub(body: {
    errors?: unknown[];
    advisories?: { key: string; message: string }[];
  }) {
    const hits = { count: 0 };
    server.use(
      http.post(VALIDATE_URL, () => {
        hits.count += 1;
        return HttpResponse.json({
          errors: body.errors ?? [],
          advisories: body.advisories ?? [],
        });
      }),
    );
    return hits;
  }

  /**
   * Drive CM6's debounced linter the way the gutter tests above do: enter edit
   * mode, focus the contenteditable, type one character. The extension list
   * debounces at 500ms (codemirror-config.ts), so callers wait with a generous
   * ceiling rather than faking timers — CM6's linter uses real timers
   * internally and faking them wedges its promise chain.
   */
  async function triggerLint(user: ReturnType<typeof userEvent.setup>) {
    const content = await findEditorContent();
    await user.click(screen.getByRole("button", { name: /^edit$/i }));
    content.focus();
    await user.keyboard("x");
  }

  test("renders the dotted key and the backend's message for every advisory", async () => {
    defaultMocks();
    validateStub({ advisories: [AUTOTAG_ADVISORY, SINGLETONS_ADVISORY] });
    const user = userEvent.setup();
    renderPage();
    await triggerLint(user);

    const key = await screen.findByText(
      AUTOTAG_ADVISORY.key,
      {},
      { timeout: 4000 },
    );
    // The dotted key is a config path, so it renders monospaced — same
    // treatment as the config file path in the section header.
    expect(key).toHaveClass("font-mono");
    // Verbatim backend copy, not a paraphrase or a truncation.
    expect(screen.getByText(AUTOTAG_ADVISORY.message)).toBeInTheDocument();
    // Both rows, not just the first: the response is a list.
    expect(screen.getByText(SINGLETONS_ADVISORY.key)).toBeInTheDocument();
    expect(screen.getByText(SINGLETONS_ADVISORY.message)).toBeInTheDocument();
  });

  test("renders no advisory UI at all when the advisories list is empty", async () => {
    defaultMocks();
    const hits = validateStub({ advisories: [] });
    const user = userEvent.setup();
    renderPage();
    await triggerLint(user);

    // Positive control first — the linter really did fire and the page really
    // did consume a ValidateResponse. Only then is the absence below evidence
    // of an empty-state branch rather than of a test that raced the debounce.
    await waitFor(() => expect(hits.count).toBeGreaterThan(0), {
      timeout: 4000,
    });
    expect(
      screen.queryByRole("list", { name: /configuration advisories/i }),
    ).not.toBeInTheDocument();
  });

  test("an advisory lands in the status channel, NOT the error channel", async () => {
    defaultMocks();
    validateStub({ errors: [], advisories: [AUTOTAG_ADVISORY] });
    const user = userEvent.setup();
    renderPage();
    await triggerLint(user);

    const key = await screen.findByText(
      AUTOTAG_ADVISORY.key,
      {},
      { timeout: 4000 },
    );
    // StatusBanner's neutral tone is ambient — role="status", never
    // role="alert". Asserting the ancestor role (rather than a class or a
    // colour) keeps this pinned to the semantic channel, not the styling.
    expect(key.closest('[role="status"]')).not.toBeNull();
    expect(key.closest('[role="alert"]')).toBeNull();
    // ...and the error channel stayed empty on the very same validate tick:
    // no gutter marker, no disabled-Save reason, Save still clickable. An
    // advisory-only config is valid and must remain saveable.
    expect(document.querySelector(".cm-lint-marker-error")).toBeNull();
    expect(screen.queryByText(/validation error/i)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^save$/i })).toBeEnabled();
  });

  test("a response carrying both channels shows the advisory AND the lint error", async () => {
    defaultMocks();
    validateStub({
      errors: [COPY_LINT_ERROR],
      advisories: [AUTOTAG_ADVISORY],
    });
    const user = userEvent.setup();
    renderPage();
    await triggerLint(user);

    // Error channel: gutter marker + the disabled-Save reason.
    await waitFor(
      () => {
        expect(document.querySelector(".cm-lint-marker-error")).not.toBeNull();
      },
      { timeout: 4000 },
    );
    expect(screen.getByText(/1 validation error/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^save$/i })).toBeDisabled();

    // Advisory channel: same response, still its own surface. Neither channel
    // suppresses the other.
    const key = screen.getByText(AUTOTAG_ADVISORY.key);
    expect(key.closest('[role="status"]')).not.toBeNull();
    expect(screen.getByText(AUTOTAG_ADVISORY.message)).toBeInTheDocument();
  });
});
