import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { client } from "@/api/client";
import type { components, operations } from "@/api/schema";
import { NamingPanel } from "@/pages/settings/NamingPanel";

type NamingSave422 =
  operations["save_naming_route_api_config_naming_save_post"]["responses"][422]["content"]["application/json"];
type ErrorDetail = components["schemas"]["ErrorDetail"];
type ReplaceError = components["schemas"]["ReplaceError"];
type NamingConfig = components["schemas"]["NamingConfig"];
type Rule = components["schemas"]["ReplaceRuleInput"];

function wrap(ui: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

// `beets_replace: []` (beets' defaults unread) keeps the rules warning out of
// every test that is not about it; the rules tests below set their own.
const naming = {
  default: "$albumartist/$album/$track $title",
  comp: null,
  singleton: null,
  custom: [],
  replace: [],
  beets_replace: [],
  sha256: "sha-1",
  previews: [
    {
      query: "default",
      sample_path: "Adele/25/01 Hello.flac",
      sample_source: "Adele — 25",
      error: null,
    },
  ],
  replace_errors: [],
} satisfies NamingConfig;

beforeEach(() => {
  vi.spyOn(client, "GET").mockImplementation(async (path: string) => {
    if (path === "/api/config/naming")
      return { data: naming, response: { ok: true, status: 200 } } as never;
    if (path === "/api/imports/active")
      return {
        data: { active: false },
        response: { ok: true, status: 200 },
      } as never;
    if (path === "/api/config")
      return {
        data: { apply_pending: false },
        response: { ok: true, status: 200 },
      } as never;
    return { data: undefined, response: { ok: false, status: 404 } } as never;
  });
  vi.spyOn(client, "POST").mockImplementation(async (path: string) => {
    if (path === "/api/config/naming/preview")
      return {
        data: { rendered: naming.previews, replace_errors: [] },
        response: { ok: true, status: 200 },
      } as never;
    if (path === "/api/config/naming/save")
      return {
        data: { apply_pending: true },
        response: { ok: true, status: 200 },
      } as never;
    return { data: undefined, response: { ok: false, status: 404 } } as never;
  });
});

afterEach(() => vi.restoreAllMocks());

test("renders current default + its live preview", async () => {
  wrap(<NamingPanel />);
  expect(
    await screen.findByDisplayValue(/\$albumartist\/\$album/),
  ).toBeInTheDocument();
  expect(screen.getByText(/Adele\/25\/01 Hello\.flac/)).toBeInTheDocument();
});

test("Save posts the assembled rules with the base sha", async () => {
  wrap(<NamingPanel />);
  const def = await screen.findByDisplayValue(/\$albumartist/);
  // Save is gated on a real change — make one so the button enables.
  await userEvent.type(def, "X");
  await userEvent.click(screen.getByRole("button", { name: /save naming/i }));
  await waitFor(() =>
    expect(client.POST).toHaveBeenCalledWith(
      "/api/config/naming/save",
      expect.objectContaining({
        body: expect.objectContaining({ base_sha256: "sha-1" }),
      }),
    ),
  );
});

test("Save is disabled until the config actually changes", async () => {
  wrap(<NamingPanel />);
  const def = await screen.findByDisplayValue(/\$albumartist/);
  const saveBtn = screen.getByRole("button", { name: /save naming/i });
  // No edits yet -> nothing to save, so the button is inert (no more spamming
  // an unchanged Save).
  expect(saveBtn).toBeDisabled();
  await userEvent.type(def, "X");
  expect(saveBtn).toBeEnabled();
});

test("Add rule reveals a custom query input", async () => {
  wrap(<NamingPanel />);
  await screen.findByDisplayValue(/\$albumartist/);
  await userEvent.click(screen.getByRole("button", { name: /add rule/i }));
  expect(screen.getByLabelText(/custom rule 1 query/i)).toBeInTheDocument();
});

// With no explicit paths:/replace: on disk, the backend now returns beets'
// effective defaults (default/comp/singleton + replace rows). The panel must
// pre-fill those — not blanks — so it's a faithful, editable view of the
// active naming.
test("pre-fills with beets' effective defaults (no-override case)", async () => {
  const effective = {
    default: "$albumartist/$album%aunique{}/$track $title",
    comp: "Compilations/$album%aunique{}/$track $title",
    singleton: "Non-Album/$artist/$title",
    custom: [],
    replace: [
      { pattern: "[<>:\\?\\*\\|]", replacement: "_" },
      { pattern: "^-", replacement: "_" },
      { pattern: "\\s+$", replacement: "" },
    ],
    // A no-block install's rows ARE beets' rules (here, three of them).
    beets_replace: [
      { pattern: "[<>:\\?\\*\\|]", replacement: "_" },
      { pattern: "^-", replacement: "_" },
      { pattern: "\\s+$", replacement: "" },
    ],
    sha256: "sha-eff",
    previews: [],
    replace_errors: [],
  } satisfies NamingConfig;
  vi.spyOn(client, "GET").mockImplementation(async (path: string) => {
    if (path === "/api/config/naming")
      return { data: effective, response: { ok: true, status: 200 } } as never;
    if (path === "/api/imports/active")
      return {
        data: { active: false },
        response: { ok: true, status: 200 },
      } as never;
    if (path === "/api/config")
      return {
        data: { apply_pending: false },
        response: { ok: true, status: 200 },
      } as never;
    return { data: undefined, response: { ok: false, status: 404 } } as never;
  });

  wrap(<NamingPanel />);

  expect(
    await screen.findByDisplayValue(
      "$albumartist/$album%aunique{}/$track $title",
    ),
  ).toBeInTheDocument();
  expect(
    screen.getByDisplayValue("Compilations/$album%aunique{}/$track $title"),
  ).toBeInTheDocument();
  expect(
    screen.getByDisplayValue("Non-Album/$artist/$title"),
  ).toBeInTheDocument();
  // A bundled replace row pre-fills too (no blank panel).
  expect(screen.getByDisplayValue("^-")).toBeInTheDocument();
});

// ---- beets' own replace rules: the warning and "Add recommended rules" ----
//
// A replace: block REPLACES beets' built-in rules, so an install made from the
// old five-rule starter lacks '[\\/]': _ and files an album with no artist or
// album tag outside the library. The read sends beets' own rules
// (`beets_replace`); the panel warns while the rows leave any out, and the
// button restores them after the typographic rules.

/** MusicDrop's five typographic rules, in their curated order. Patterns are
 * literal \uXXXX text so they stay legible in the editor. */
const TYPOGRAPHIC: Rule[] = [
  { pattern: String.raw`[\u2010\u2011\u2212]`, replacement: "-" },
  { pattern: String.raw`[\u2013\u2014]`, replacement: "-" },
  { pattern: String.raw`[\u2018\u2019\u02bc]`, replacement: "'" },
  { pattern: String.raw`[\u201c\u201d]`, replacement: "_" },
  { pattern: String.raw`\u2026`, replacement: "..." },
];
/** beets' own rules as the read sends them, in beets 2.14's order
 * (beets/config_default.yaml). A fixture: the panel reads them only from the
 * response. */
const BEETS: Rule[] = [
  { pattern: String.raw`[<>:\?\*\|]`, replacement: "_" },
  { pattern: String.raw`\"`, replacement: "_" },
  { pattern: String.raw`[\\/]`, replacement: "_" },
  { pattern: String.raw`^\.`, replacement: "_" },
  { pattern: String.raw`\.$`, replacement: "_" },
  { pattern: String.raw`[\x00-\x1f]`, replacement: "_" },
  { pattern: "^-", replacement: "_" },
  { pattern: String.raw`\s+$`, replacement: "" },
  { pattern: String.raw`^\s+`, replacement: "" },
];
const SEPARATOR = BEETS[2];
const LEADING_DASH = BEETS[6];
/** The new starter config's block (backend/app/beets/config.starter.yaml). */
const STARTER: Rule[] = [...TYPOGRAPHIC, ...BEETS];
const without = (rows: Rule[], gone: Rule) =>
  rows.filter((r) => r.pattern !== gone.pattern);

const WARN_WITH_RISK =
  "Some of beets’ own replace rules are missing, so an album with no artist or album tag can be filed outside your library. Add recommended rules puts them back.";
const WARN_NO_RISK =
  "Some of beets’ own replace rules are missing. Add recommended rules puts them back.";
const NOTHING_TO_ADD = "Recommended rules are already in place.";

/** Render the panel on a read whose rows are `replace` and whose beets rules
 * are `beetsReplace`; resolve once the editor is up. */
async function renderRules(replace: Rule[], beetsReplace: Rule[] = BEETS) {
  type Stub = (path: string) => Promise<unknown>;
  const get = vi.mocked(client.GET).getMockImplementation() as unknown as
    | Stub
    | undefined;
  if (!get) throw new Error("beforeEach mocks missing");
  const read: NamingConfig = {
    ...naming,
    replace,
    beets_replace: beetsReplace,
    sha256: "sha-rules",
  };
  vi.mocked(client.GET).mockImplementation((async (path: string) =>
    path === "/api/config/naming"
      ? { data: read, response: { ok: true, status: 200 } }
      : get(path)) as never);
  wrap(<NamingPanel />);
  await screen.findByDisplayValue(/\$albumartist/);
}

/** The replace rows in the editor, in order. */
function replaceRows(): Rule[] {
  const values = screen.queryAllByLabelText(/^Replace value \d+$/);
  return screen
    .queryAllByLabelText(/^Replace pattern \d+$/)
    .map((p, i) => ({
      pattern: (p as HTMLInputElement).value,
      replacement: (values[i] as HTMLInputElement).value,
    }));
}

/** The whole rules warning, or null when it is not shown. */
function rulesWarning(): string | null {
  const line = screen.queryByText(/beets’ own replace rules are missing/);
  if (!line) return null;
  const banner = line.closest('[data-slot="status-banner"]');
  if (!banner) throw new Error("the rules warning is not in a banner");
  return banner.textContent;
}

const addRecommended = () =>
  userEvent.click(
    screen.getByRole("button", { name: /add recommended rules/i }),
  );

test("the old starter's rows warn, naming the risk: beets' separator rule is missing", async () => {
  await renderRules(TYPOGRAPHIC);
  expect(rulesWarning()).toBe(WARN_WITH_RISK);
  expect(screen.getByRole("alert")).toHaveTextContent(WARN_WITH_RISK);
});

test("the warning leaves the risk out while the separator rule is present", async () => {
  await renderRules(without(STARTER, LEADING_DASH));
  expect(rulesWarning()).toBe(WARN_NO_RISK);
});

test("no warning on an install with no replace: block (its rows are beets' rules)", async () => {
  await renderRules(BEETS);
  expect(replaceRows()).toEqual(BEETS);
  expect(rulesWarning()).toBeNull();
});

test("a row with beets' pattern and the user's own replacement counts as present", async () => {
  const own = STARTER.map((r) =>
    r.pattern === SEPARATOR.pattern ? { ...r, replacement: "-" } : r,
  );
  await renderRules(own);
  expect(replaceRows()).toEqual(own);
  expect(rulesWarning()).toBeNull();
});

test("no warning when beets' own rules could not be read", async () => {
  await renderRules(TYPOGRAPHIC, []);
  expect(replaceRows()).toEqual(TYPOGRAPHIC);
  expect(rulesWarning()).toBeNull();
});

test("deleting the separator row warns; Add recommended rules puts it back and the warning goes", async () => {
  await renderRules(BEETS);
  expect(rulesWarning()).toBeNull();
  await userEvent.click(
    screen.getByRole("button", { name: "Remove replace rule 3" }),
  );
  expect(replaceRows()).toEqual(without(BEETS, SEPARATOR));
  expect(rulesWarning()).toBe(WARN_WITH_RISK);

  await addRecommended();
  expect(replaceRows()).toEqual(STARTER);
  expect(rulesWarning()).toBeNull();
});

test("Add recommended rules on a no-block install gives the starter's block", async () => {
  await renderRules(BEETS);
  await addRecommended();
  expect(replaceRows()).toEqual(STARTER);
});

test("Add recommended rules on the old starter gives the new starter's block", async () => {
  await renderRules(TYPOGRAPHIC);
  await addRecommended();
  expect(replaceRows()).toEqual(STARTER);
  expect(rulesWarning()).toBeNull();
  expect(screen.queryByText(NOTHING_TO_ADD)).not.toBeInTheDocument();
});

test("Add recommended rules moves beets' rules after the typographic ones (the old button's order)", async () => {
  await renderRules([...BEETS, ...TYPOGRAPHIC]);
  await addRecommended();
  expect(replaceRows()).toEqual(STARTER);
});

test("Add recommended rules keeps the user's other rows, in order, between the two sets", async () => {
  const amp = { pattern: "&", replacement: "and" };
  const the = { pattern: "^The ", replacement: "" };
  await renderRules([SEPARATOR, amp, TYPOGRAPHIC[4], the]);
  await addRecommended();
  expect(replaceRows()).toEqual([...TYPOGRAPHIC, amp, the, ...BEETS]);
});

test("Add recommended rules keeps the user's replacement on a matching pattern", async () => {
  await renderRules([
    { pattern: SEPARATOR.pattern, replacement: "-" },
    { pattern: TYPOGRAPHIC[2].pattern, replacement: "_" },
  ]);
  await addRecommended();
  expect(replaceRows()).toEqual([
    ...TYPOGRAPHIC.slice(0, 2),
    { pattern: TYPOGRAPHIC[2].pattern, replacement: "_" },
    ...TYPOGRAPHIC.slice(3),
    ...BEETS.slice(0, 2),
    { pattern: SEPARATOR.pattern, replacement: "-" },
    ...BEETS.slice(3),
  ]);
});

test("Add recommended rules keeps both rows of a pattern the user typed twice", async () => {
  const amp = { pattern: "&", replacement: "and" };
  const first = { pattern: SEPARATOR.pattern, replacement: "_" };
  const second = { pattern: SEPARATOR.pattern, replacement: "-" };
  await renderRules([first, amp, second]);
  const expected = [
    ...TYPOGRAPHIC,
    amp,
    ...BEETS.slice(0, 2),
    first,
    second,
    ...BEETS.slice(3),
  ];
  await addRecommended();
  expect(replaceRows()).toEqual(expected);
  await addRecommended();
  expect(replaceRows()).toEqual(expected);
});

test("pressing Add recommended rules twice equals pressing it once", async () => {
  await renderRules(TYPOGRAPHIC);
  await addRecommended();
  const once = replaceRows();
  expect(once).toEqual(STARTER);
  await addRecommended();
  expect(replaceRows()).toEqual(once);
});

test("with beets' rules unread, Add recommended rules still puts the typographic rules first", async () => {
  const amp = { pattern: "&", replacement: "and" };
  await renderRules([amp, TYPOGRAPHIC[1]], []);
  await addRecommended();
  expect(replaceRows()).toEqual([...TYPOGRAPHIC, amp]);
});

test("a press that would change nothing says so, and the line goes with the next edit", async () => {
  await renderRules(STARTER);
  expect(screen.queryByText(NOTHING_TO_ADD)).not.toBeInTheDocument();
  await addRecommended();
  // Polite, not an alert: nothing is wrong.
  expect(screen.getByText(NOTHING_TO_ADD).tagName).toBe("OUTPUT");
  expect(replaceRows()).toEqual(STARTER);
  // Nothing changed, so there is still nothing to save.
  expect(screen.getByRole("button", { name: /save naming/i })).toBeDisabled();

  await userEvent.type(screen.getByLabelText("Replace value 1"), "x");
  expect(screen.queryByText(NOTHING_TO_ADD)).not.toBeInTheDocument();
});

test("Save sends the restored rules", async () => {
  await renderRules(TYPOGRAPHIC);
  await addRecommended();
  await userEvent.click(screen.getByRole("button", { name: /save naming/i }));
  await waitFor(() =>
    expect(client.POST).toHaveBeenCalledWith(
      "/api/config/naming/save",
      expect.objectContaining({
        body: expect.objectContaining({ replace: STARTER }),
      }),
    ),
  );
});

/** Render with a saved-but-unapplied config and an Apply that fails with
 * `status` and `error`, click Apply, and return the alert it raises. */
async function applyFails(status: number, error: unknown) {
  // The beforeEach stubs, re-typed to the one argument they read.
  type Stub = (path: string) => Promise<unknown>;
  const get = vi.mocked(client.GET).getMockImplementation() as unknown as
    | Stub
    | undefined;
  const post = vi.mocked(client.POST).getMockImplementation() as unknown as
    | Stub
    | undefined;
  if (!get || !post) throw new Error("beforeEach mocks missing");
  vi.mocked(client.GET).mockImplementation((async (path: string) =>
    path === "/api/config"
      ? { data: { apply_pending: true }, response: { ok: true, status: 200 } }
      : get(path)) as never);
  vi.mocked(client.POST).mockImplementation((async (path: string) =>
    path === "/api/config/apply"
      ? { data: undefined, error, response: { ok: false, status } }
      : post(path)) as never);
  wrap(<NamingPanel />);
  const applyBtn = await screen.findByRole("button", { name: "Apply" });
  await waitFor(() => expect(applyBtn).toBeEnabled());
  await userEvent.click(applyBtn);
  return screen.findByRole("alert");
}

test("an Apply 422 shows the server's recovery sentence, not the restart advice", async () => {
  // The unreadable-file 422: boot reads the same file, so "restart" would
  // stop MusicDrop. The panel must print what the server says.
  const alert = await applyFails(422, {
    detail: {
      message: "config.yaml could not be read",
      recovery:
        "beets could not read config.yaml, so nothing was changed. Fix the file and Apply again.",
    },
  });
  expect(alert).toHaveTextContent(
    /^Apply failed\. beets could not read config\.yaml, so nothing was changed\. Fix the file and Apply again\.$/,
  );
  // A store-layout refusal's sentence quotes two paths; the alert must wrap.
  expect(alert).toHaveClass("break-words");
});

test("an Apply failure without a recovery line falls back to the fixed sentence", async () => {
  const alert = await applyFails(422, { detail: "unexpected" });
  expect(alert).toHaveTextContent(
    /^Apply failed\. Your config is saved on disk — try again or restart MusicDrop\.$/,
  );
});

/** Edit the default template, click Save against a Save answering `status`
 * (422 unless given) with `error`, and return the alert it raises. `onReady`
 * runs once the panel has loaded. */
async function saveFails(
  error: NamingSave422,
  {
    applyPending = false,
    status = 422,
    onReady = async () => {},
  }: {
    applyPending?: boolean;
    status?: number;
    onReady?: () => Promise<void>;
  } = {},
) {
  type Stub = (path: string) => Promise<unknown>;
  const get = vi.mocked(client.GET).getMockImplementation() as unknown as
    | Stub
    | undefined;
  const post = vi.mocked(client.POST).getMockImplementation() as unknown as
    | Stub
    | undefined;
  if (!get || !post) throw new Error("beforeEach mocks missing");
  vi.mocked(client.GET).mockImplementation((async (path: string) =>
    path === "/api/config"
      ? {
          data: { apply_pending: applyPending },
          response: { ok: true, status: 200 },
        }
      : get(path)) as never);
  vi.mocked(client.POST).mockImplementation((async (path: string) =>
    path === "/api/config/naming/save"
      ? { data: undefined, error, response: { ok: false, status } }
      : post(path)) as never);
  wrap(<NamingPanel />);
  const def = await screen.findByDisplayValue(/\$albumartist/);
  await onReady();
  await userEvent.type(def, "X");
  await userEvent.click(screen.getByRole("button", { name: /save naming/i }));
  return screen.findByRole("alert");
}

const WRITE_FAULT: NamingSave422 = {
  detail: [
    {
      loc: "",
      msg: "config.yaml could not be written: Permission denied.",
      type: "config_on_disk",
    },
  ],
};

test("a Save 422 about config.yaml on disk shows the server's sentence, nothing appended", async () => {
  const alert = await saveFails(WRITE_FAULT);
  expect(alert).toHaveTextContent(
    /^Save failed\. config\.yaml could not be written: Permission denied\.$/,
  );
  // The sentence is the server's, not ours: the alert wraps whatever it sends.
  expect(alert).toHaveClass("break-words");
});

test("the Save sentence follows the row's type, not its text", async () => {
  const alert = await saveFails({
    detail: [{ ...WRITE_FAULT.detail[0], type: "value_error" }],
  });
  expect(alert).toHaveTextContent(
    /^Save failed\. Your changes weren’t written — try again\.$/,
  );
});

test("a failed Save hides the Saved cue", async () => {
  const alert = await saveFailsTypedBack(fail(422, WRITE_FAULT));
  expect(alert).toHaveTextContent(/^Save failed\./);
  expect(screen.queryByText(/^Saved\. Click/)).not.toBeInTheDocument();
});

test("a Save conflict hides the Saved cue", async () => {
  const banner = await saveFailsTypedBack(fail(409, { detail: [] }));
  expect(banner).toHaveTextContent(/your save was refused/);
  expect(screen.queryByText(/^Saved\. Click/)).not.toBeInTheDocument();
});

type Reply = {
  data?: unknown;
  error?: unknown;
  response: { ok: boolean; status: number };
};
const ok = (data: unknown): Reply => ({
  data,
  response: { ok: true, status: 200 },
});
const fail = (status: number, error?: unknown): Reply => ({
  data: undefined,
  error,
  response: { ok: false, status },
});
/** A reply that never comes: the action stays in flight. */
const never = () => new Promise<Reply>(() => {});

/** A reply that comes only when `answer` is called. */
function held() {
  let answer: (r: Reply) => void = () => {};
  const reply = () =>
    new Promise<Reply>((resolve) => {
      answer = resolve;
    });
  return { reply, answer: (r: Reply) => answer(r) };
}

/** Replace the beforeEach stubs whole. Each route answers from its callback
 * at request time, so a test can change the answer mid-test. */
function mockPanel({
  applyPending = true,
  jobActive = () => false,
  save = () => ok({ apply_pending: true }),
  apply = () => ok({ apply_pending: false }),
  replaceErrors = () => [],
}: {
  applyPending?: boolean;
  jobActive?: () => boolean;
  save?: () => Reply | Promise<Reply>;
  apply?: () => Reply | Promise<Reply>;
  replaceErrors?: () => ReplaceError[];
} = {}) {
  const hits = { preview: 0, probe: 0, apply: 0 };
  vi.mocked(client.GET).mockImplementation((async (path: string) => {
    if (path === "/api/config/naming") return ok(naming);
    if (path === "/api/imports/active") {
      hits.probe += 1;
      return ok({ active: jobActive() });
    }
    if (path === "/api/config") return ok({ apply_pending: applyPending });
    return fail(404);
  }) as never);
  vi.mocked(client.POST).mockImplementation((async (path: string) => {
    if (path === "/api/config/naming/preview") {
      hits.preview += 1;
      return ok({ rendered: naming.previews, replace_errors: replaceErrors() });
    }
    if (path === "/api/config/naming/save") return save();
    if (path === "/api/config/apply") {
      hits.apply += 1;
      return apply();
    }
    return fail(404);
  }) as never);
  return hits;
}

/** Everything the panel offers and says, pinned whole: each button's label
 * (" (off)" when disabled), the lines beside them, what sits below them, and
 * every alert. */
function namingState() {
  const button = (name: RegExp) => {
    const b = screen.getByRole("button", { name }) as HTMLButtonElement;
    return `${b.textContent}${b.disabled ? " (off)" : ""}`;
  };
  const saveBtn = screen.getByRole("button", { name: /^(save naming|saving…)$/i });
  const footer = saveBtn.parentElement;
  if (!footer) throw new Error("no footer");
  const below: string[] = [];
  for (let e = footer.nextElementSibling; e; e = e.nextElementSibling) {
    below.push(e.textContent);
  }
  return {
    save: button(/^(save naming|saving…)$/i),
    apply: button(/^(apply|applying…)$/i),
    lines: Array.from(footer.querySelectorAll("p, output"), (e) => e.textContent),
    below,
    alerts: screen.queryAllByRole("alert").map((e) => e.textContent),
  };
}

const CUE = "Saved. Click Apply to load it.";
const UNSAVED = "Unsaved changes. Save, then Apply.";
const SAVE_FALLBACK_ALERT =
  "Save failed. Your changes weren’t written — try again.";
const APPLY_FALLBACK_ALERT =
  "Apply failed. Your config is saved on disk — try again or restart MusicDrop.";
const UNREADABLE = {
  detail: {
    message: "config.yaml could not be read",
    recovery:
      "beets could not read config.yaml, so nothing was changed. Fix the file and Apply again.",
  },
};
const REFUSED_ALERT =
  "Apply failed. beets could not read config.yaml, so nothing was changed. Fix the file and Apply again.";

/** Type a draft, Save it, and type the draft back to the file while that Save
 * runs; then the Save answers `answer`. A Save fails with its draft open, and
 * a draft alone hides the cue and turns Apply off, so this is the one way the
 * cue and Apply meet a failed Save. Returns the first alert. */
async function saveFailsTypedBack(answer: Reply) {
  const save = held();
  mockPanel({ save: save.reply });
  wrap(<NamingPanel />);
  const def = await screen.findByDisplayValue(/\$albumartist/);
  // The cue shows while nothing has failed.
  expect(await screen.findByText(/^Saved\. Click/)).toBeInTheDocument();
  await userEvent.type(def, "X");
  await userEvent.click(screen.getByRole("button", { name: /save naming/i }));
  await userEvent.type(def, "{backspace}");
  save.answer(answer);
  return screen.findByRole("alert");
}

test("the Save alert gives way to an invalid replace pattern, and stays gone once it is fixed", async () => {
  let replaceErrors: ReplaceError[] = [];
  const hits = mockPanel({
    applyPending: false,
    save: () =>
      fail(422, {
        detail: [
          { loc: "replace[0]", msg: "unterminated set", type: "value_error" },
        ],
      }),
    replaceErrors: () => replaceErrors,
  });
  wrap(<NamingPanel />);
  const def = await screen.findByDisplayValue(/\$albumartist/);
  await waitFor(() => expect(hits.preview).toBeGreaterThan(0));

  // Save inside the 250 ms preview delay: nothing is marked yet.
  replaceErrors = [{ index: 0, pattern: "[", message: "unterminated set" }];
  await userEvent.type(def, "X");
  await userEvent.click(screen.getByRole("button", { name: /save naming/i }));
  expect(await screen.findByRole("alert")).toHaveTextContent(
    SAVE_FALLBACK_ALERT,
  );

  // The preview marks it: the replace line is the one recovery.
  await waitFor(() =>
    expect(namingState()).toEqual({
      save: "Save naming (off)",
      apply: "Apply (off)",
      lines: ["Invalid replace pattern. Fix to save."],
      below: [],
      alerts: [],
    }),
  );

  // Fixed: the line goes, and the old alert does not come back.
  replaceErrors = [];
  await userEvent.type(def, "Y");
  await waitFor(() =>
    expect(namingState()).toEqual({
      save: "Save naming",
      apply: "Apply (off)",
      lines: [UNSAVED],
      below: [],
      alerts: [],
    }),
  );
});

test("a Save failure, then an Apply failure, shows only the Apply alert", async () => {
  // Apply is off beside a draft, so the draft is typed back to the file while
  // the Save runs: the one way to reach Apply with a Save failure showing.
  const save = held();
  mockPanel({ save: save.reply, apply: () => fail(502) });
  wrap(<NamingPanel />);
  const def = await screen.findByDisplayValue(/\$albumartist/);
  await userEvent.type(def, "X");
  await userEvent.click(screen.getByRole("button", { name: /save naming/i }));
  await userEvent.type(def, "{backspace}");
  save.answer(fail(500));
  await waitFor(() =>
    expect(namingState()).toEqual({
      save: "Save naming (off)",
      apply: "Apply",
      lines: [],
      below: [SAVE_FALLBACK_ALERT],
      alerts: [SAVE_FALLBACK_ALERT],
    }),
  );

  await userEvent.click(screen.getByRole("button", { name: "Apply" }));
  await waitFor(() =>
    expect(namingState()).toEqual({
      save: "Save naming (off)",
      apply: "Apply",
      lines: [],
      below: [APPLY_FALLBACK_ALERT],
      alerts: [APPLY_FALLBACK_ALERT],
    }),
  );
});

test("an Apply failure, then a Save failure, shows only the Save alert", async () => {
  mockPanel({ save: () => fail(500), apply: () => fail(422, UNREADABLE) });
  wrap(<NamingPanel />);
  const def = await screen.findByDisplayValue(/\$albumartist/);
  await userEvent.click(await screen.findByRole("button", { name: "Apply" }));
  await screen.findByText(REFUSED_ALERT);

  await userEvent.type(def, "X");
  await userEvent.click(screen.getByRole("button", { name: /save naming/i }));
  await waitFor(() =>
    expect(namingState()).toEqual({
      save: "Save naming",
      apply: "Apply (off)",
      lines: [],
      below: [SAVE_FALLBACK_ALERT],
      alerts: [SAVE_FALLBACK_ALERT],
    }),
  );
});

test("an Apply failure hides the Saved cue: the alert carries the recovery", async () => {
  mockPanel({ apply: () => fail(422, UNREADABLE) });
  wrap(<NamingPanel />);
  await screen.findByDisplayValue(/\$albumartist/);
  // The cue shows while nothing has failed.
  await waitFor(() =>
    expect(namingState()).toEqual({
      save: "Save naming (off)",
      apply: "Apply",
      lines: [CUE],
      below: [],
      alerts: [],
    }),
  );

  await userEvent.click(screen.getByRole("button", { name: "Apply" }));
  await waitFor(() =>
    expect(namingState()).toEqual({
      save: "Save naming (off)",
      apply: "Apply",
      lines: [],
      below: [REFUSED_ALERT],
      alerts: [REFUSED_ALERT],
    }),
  );
});

test("an Apply 409 asks the job probes again, and the paused line speaks for the job", async () => {
  // The job started elsewhere after this panel last asked.
  let running = false;
  mockPanel({
    apply: () => {
      running = true;
      return fail(409, { detail: "a library job is running" });
    },
    jobActive: () => running,
  });
  wrap(<NamingPanel />);
  await screen.findByDisplayValue(/\$albumartist/);
  await userEvent.click(await screen.findByRole("button", { name: "Apply" }));

  await waitFor(() =>
    expect(namingState()).toEqual({
      save: "Save naming (off)",
      apply: "Apply (off)",
      lines: ["Apply paused: an import is running; available when it finishes."],
      below: [],
      alerts: [],
    }),
  );
});

test("an Apply 409 after the job has ended leaves nothing about a job", async () => {
  const hits = mockPanel({
    apply: () => fail(409, { detail: "a library job is running" }),
  });
  wrap(<NamingPanel />);
  await screen.findByDisplayValue(/\$albumartist/);
  const applyBtn = await screen.findByRole("button", { name: "Apply" });
  await waitFor(() => expect(hits.probe).toBeGreaterThan(0));
  const probesBefore = hits.probe;
  await userEvent.click(applyBtn);

  await waitFor(() => expect(hits.apply).toBe(1));
  await waitFor(() => expect(hits.probe).toBeGreaterThan(probesBefore));
  await waitFor(() =>
    expect(namingState()).toEqual({
      save: "Save naming (off)",
      apply: "Apply",
      lines: [CUE],
      below: [],
      alerts: [],
    }),
  );
});

/** Pending, nothing typed: the cue shows and Apply is on. */
const PENDING_REST = {
  save: "Save naming (off)",
  apply: "Apply",
  lines: [CUE],
  below: [],
  alerts: [],
};

test("a draft swaps the Saved cue for the unsaved line and turns Apply off; typing it back restores both", async () => {
  mockPanel();
  wrap(<NamingPanel />);
  const def = await screen.findByDisplayValue(/\$albumartist/);
  await waitFor(() => expect(namingState()).toEqual(PENDING_REST));

  // Apply would load the file, not the draft.
  await userEvent.type(def, "X");
  await waitFor(() =>
    expect(namingState()).toEqual({
      save: "Save naming",
      apply: "Apply (off)",
      lines: [UNSAVED],
      below: [],
      alerts: [],
    }),
  );

  await userEvent.type(def, "{backspace}");
  await waitFor(() => expect(namingState()).toEqual(PENDING_REST));
});

test("Apply is off while a Save is in flight", async () => {
  mockPanel({ save: never });
  wrap(<NamingPanel />);
  const def = await screen.findByDisplayValue(/\$albumartist/);
  await userEvent.type(def, "X");
  await userEvent.click(screen.getByRole("button", { name: /save naming/i }));
  const saving = {
    save: "Saving… (off)",
    apply: "Apply (off)",
    lines: [],
    below: [],
    alerts: [],
  };
  expect(namingState()).toEqual(saving);

  // The draft typed back to the file: only the Save in flight keeps Apply off.
  await userEvent.type(def, "{backspace}");
  await new Promise((r) => setTimeout(r, 50));
  expect(namingState()).toEqual(saving);
});

test("Save is off while an Apply is in flight, and the cue is gone", async () => {
  mockPanel({ apply: never });
  wrap(<NamingPanel />);
  const def = await screen.findByDisplayValue(/\$albumartist/);
  await waitFor(() => expect(namingState()).toEqual(PENDING_REST));

  await userEvent.click(screen.getByRole("button", { name: "Apply" }));
  const applying = {
    save: "Save naming (off)",
    apply: "Applying… (off)",
    lines: [],
    below: [],
    alerts: [],
  };
  expect(namingState()).toEqual(applying);

  // A draft typed while Apply runs: Save stays off, and the draft is still
  // unsaved once Apply ends.
  await userEvent.type(def, "X");
  await new Promise((r) => setTimeout(r, 50));
  expect(namingState()).toEqual({ ...applying, lines: [UNSAVED] });
});

test("the unsaved line shows with nothing waiting to be applied, and not at rest", async () => {
  mockPanel({ applyPending: false });
  wrap(<NamingPanel />);
  const def = await screen.findByDisplayValue(/\$albumartist/);
  const rest = {
    save: "Save naming (off)",
    apply: "Apply (off)",
    lines: [],
    below: [],
    alerts: [],
  };
  await new Promise((r) => setTimeout(r, 50));
  expect(namingState()).toEqual(rest);

  await userEvent.type(def, "X");
  await waitFor(() =>
    expect(namingState()).toEqual({
      ...rest,
      save: "Save naming",
      lines: [UNSAVED],
    }),
  );
});

test("the unsaved line gives way to an invalid replace pattern", async () => {
  let replaceErrors: ReplaceError[] = [];
  const hits = mockPanel({ replaceErrors: () => replaceErrors });
  wrap(<NamingPanel />);
  const def = await screen.findByDisplayValue(/\$albumartist/);
  await waitFor(() => expect(hits.preview).toBeGreaterThan(0));

  replaceErrors = [{ index: 0, pattern: "[", message: "unterminated set" }];
  await userEvent.type(def, "X");
  await waitFor(() =>
    expect(namingState()).toEqual({
      save: "Save naming (off)",
      apply: "Apply (off)",
      lines: ["Invalid replace pattern. Fix to save."],
      below: [],
      alerts: [],
    }),
  );
});

test("a draft replaces the paused line: Apply waits for the Save, not the job", async () => {
  mockPanel({ jobActive: () => true });
  wrap(<NamingPanel />);
  const def = await screen.findByDisplayValue(/\$albumartist/);
  await screen.findByText(/^Apply paused:/);

  await userEvent.type(def, "X");
  await waitFor(() =>
    expect(namingState()).toEqual({
      save: "Save naming",
      apply: "Apply (off)",
      lines: [UNSAVED],
      below: [],
      alerts: [],
    }),
  );
});

test("the unsaved line shows beside an Apply failure's recovery", async () => {
  // Decision #59: while a draft is unsaved, the line says so. No Apply
  // recovery sentence contradicts "Save, then Apply."
  mockPanel({ apply: () => fail(422, UNREADABLE) });
  wrap(<NamingPanel />);
  const def = await screen.findByDisplayValue(/\$albumartist/);
  await userEvent.click(await screen.findByRole("button", { name: "Apply" }));
  await screen.findByText(REFUSED_ALERT);

  await userEvent.type(def, "X");
  await waitFor(() =>
    expect(namingState()).toEqual({
      save: "Save naming",
      apply: "Apply (off)",
      lines: [UNSAVED],
      below: [REFUSED_ALERT],
      alerts: [REFUSED_ALERT],
    }),
  );
});

test("a row Save would not send is not a change; a rule with a query and a template is", async () => {
  // Save leaves these rows out, so its write would be the same bytes, the
  // sha would not move and the panel would not remount: a draft counted from
  // the rows would keep Apply off after that Save with no line.
  const bodies: unknown[] = [];
  mockPanel();
  // mockPanel's stub, re-typed to the one argument it reads.
  type Stub = (path: string) => Promise<unknown>;
  const post = vi.mocked(client.POST).getMockImplementation() as unknown as
    | Stub
    | undefined;
  if (!post) throw new Error("mockPanel stub missing");
  vi.mocked(client.POST).mockImplementation(((path: string, init: unknown) => {
    if (path === "/api/config/naming/save") bodies.push(init);
    return post(path);
  }) as never);
  wrap(<NamingPanel />);
  await screen.findByDisplayValue(/\$albumartist/);
  await waitFor(() => expect(namingState()).toEqual(PENDING_REST));

  // An empty rule and an empty replace row.
  await userEvent.click(screen.getByRole("button", { name: /add rule/i }));
  await userEvent.click(
    screen.getByRole("button", { name: /add replacement/i }),
  );
  expect(namingState()).toEqual(PENDING_REST);

  // A template with no query: the backend writes no key for it.
  const template = screen.getByLabelText("Custom rule 1 template");
  await userEvent.type(template, "Live/$album");
  expect(namingState()).toEqual(PENDING_REST);

  // A query with a template of only spaces: Save's own trim leaves it out.
  await userEvent.click(screen.getByRole("button", { name: /add rule/i }));
  await userEvent.type(
    screen.getByLabelText("Custom rule 2 query"),
    "albumtype:ep",
  );
  await userEvent.type(screen.getByLabelText("Custom rule 2 template"), "  ");
  expect(namingState()).toEqual(PENDING_REST);

  // Control: with a query too, it is a change.
  await userEvent.type(
    screen.getByLabelText("Custom rule 1 query"),
    "albumtype:live",
  );
  const draft = {
    save: "Save naming",
    apply: "Apply (off)",
    lines: [UNSAVED],
    below: [],
    alerts: [],
  };
  expect(namingState()).toEqual(draft);

  // Save sends exactly the rows the draft was counted from.
  await userEvent.click(screen.getByRole("button", { name: /save naming/i }));
  await waitFor(() => expect(bodies).toHaveLength(1));
  expect(bodies[0]).toEqual({
    body: {
      rules: [
        { query: "default", template: naming.default },
        { query: "albumtype:live", template: "Live/$album" },
      ],
      replace: [],
      base_sha256: "sha-1",
    },
  });
});

test("the unsaved line gives way to a Save conflict, through later edits", async () => {
  mockPanel({ save: () => fail(409, { detail: [] }) });
  wrap(<NamingPanel />);
  const def = await screen.findByDisplayValue(/\$albumartist/);
  await userEvent.type(def, "X");
  await userEvent.click(screen.getByRole("button", { name: /save naming/i }));
  const banner = await screen.findByRole("alert");

  // An edit ends the Save's error; the conflict banner still speaks.
  await userEvent.type(def, "Y");
  await new Promise((r) => setTimeout(r, 50));
  expect(namingState()).toEqual({
    save: "Save naming",
    apply: "Apply (off)",
    lines: [],
    below: [],
    alerts: [banner.textContent],
  });
});

test("the paused line shows while a job holds a pending Apply", async () => {
  mockPanel({ jobActive: () => true });
  wrap(<NamingPanel />);
  await screen.findByDisplayValue(/\$albumartist/);
  await waitFor(() =>
    expect(namingState()).toEqual({
      save: "Save naming (off)",
      apply: "Apply (off)",
      lines: ["Apply paused: an import is running; available when it finishes."],
      below: [],
      alerts: [],
    }),
  );
});

test("no paused line when nothing is waiting to be applied", async () => {
  const hits = mockPanel({ applyPending: false, jobActive: () => true });
  wrap(<NamingPanel />);
  await screen.findByDisplayValue(/\$albumartist/);
  await waitFor(() => expect(hits.probe).toBeGreaterThan(0));
  await new Promise((r) => setTimeout(r, 50));
  expect(namingState()).toEqual({
    save: "Save naming (off)",
    apply: "Apply (off)",
    lines: [],
    below: [],
    alerts: [],
  });
});

test("a Save 422 for a bad replace: pattern keeps the fixed sentence", async () => {
  const alert = await saveFails({
    detail: [
      {
        loc: "replace[0]",
        msg: "unterminated character set at position 0",
        type: "value_error",
      },
    ],
  });
  expect(alert).toHaveTextContent(
    /^Save failed\. Your changes weren’t written — try again\.$/,
  );
});

/** Render against a naming GET that answers `status` + `error`; return the alert. */
async function loadFails(status: number, error: ErrorDetail) {
  vi.mocked(client.GET).mockImplementation((async () => ({
    data: undefined,
    error,
    response: { ok: false, status },
  })) as never);
  wrap(<NamingPanel />);
  return screen.findByRole("alert");
}

const PARSE_FAULT =
  "config.yaml does not parse: YAML error at line 3. Fix it in Settings → Beets.";

test("a load 422 about config.yaml on disk shows the headline, then the server's sentence", async () => {
  const alert = await loadFails(422, { detail: PARSE_FAULT });
  expect(Array.from(alert.children, (p) => p.textContent)).toEqual([
    "Could not load naming config.",
    PARSE_FAULT,
  ]);
  // The sentence is the server's, not ours: the alert wraps whatever it sends.
  expect(alert.children[1]).toHaveClass("break-words");
});

test("Try again reloads the naming config", async () => {
  type Stub = (path: string) => Promise<unknown>;
  const ok = vi.mocked(client.GET).getMockImplementation() as unknown as
    | Stub
    | undefined;
  if (!ok) throw new Error("beforeEach mocks missing");
  let failed = false;
  vi.mocked(client.GET).mockImplementation((async (path: string) => {
    if (path === "/api/config/naming" && !failed) {
      failed = true;
      return {
        data: undefined,
        error: { detail: PARSE_FAULT },
        response: { ok: false, status: 422 },
      };
    }
    return ok(path);
  }) as never);
  wrap(<NamingPanel />);
  await screen.findByRole("alert");
  await userEvent.click(screen.getByRole("button", { name: "Try again" }));
  expect(
    await screen.findByDisplayValue(/\$albumartist/),
  ).toBeInTheDocument();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

test("a load 500 keeps the fixed sentence", async () => {
  const alert = await loadFails(500, { detail: "Internal Server Error" });
  expect(alert).toHaveTextContent(/^Could not load naming config\.$/);
});

test("a load network failure keeps the fixed sentence", async () => {
  vi.mocked(client.GET).mockRejectedValue(new TypeError("Failed to fetch"));
  wrap(<NamingPanel />);
  expect(await screen.findByRole("alert")).toHaveTextContent(
    /^Could not load naming config\.$/,
  );
});
