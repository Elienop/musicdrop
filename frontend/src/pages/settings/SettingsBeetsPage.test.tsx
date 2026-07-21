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
      http.post(VALIDATE_URL, () => HttpResponse.json({ errors: [] })),
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
      http.post(VALIDATE_URL, () => HttpResponse.json({ errors: [] })),
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
      http.post(VALIDATE_URL, () => HttpResponse.json({ errors: [] })),
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
    // Re-query inside waitFor: the Loader's interim h2 carries the same name
    // and detaches when the snapshot lands, so a one-shot findBy can resolve
    // with a node the data swap then removes.
    await waitFor(() =>
      expect(
        screen.getByRole("heading", { level: 2, name: "Beets configuration" }),
      ).toBeInTheDocument(),
    );
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
