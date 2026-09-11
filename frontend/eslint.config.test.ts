// Mutation tests for `frontend/eslint.config.js`.
//
// It sits at the frontend root, beside the config it tests, rather than under `src/`.
// That is forced, not stylistic: it needs `node:fs`/`node:path` and `import.meta.dirname`,
// and `tsconfig.app.json` deliberately carries no `@types/node` — app code is browser
// code. `tsconfig.node.json` already has `"types": ["node"]`, so the file is listed in
// that project's `include` instead.
//
// Runs under the project-wide jsdom environment rather than `@vitest-environment node`:
// `vitest.config.ts` applies `setupFiles: ["./src/test/setup.ts"]` to every file
// regardless of environment, and that setup imports `@testing-library/react`, which
// cannot load without a DOM. ESLint itself is indifferent — it only needs Node builtins,
// which jsdom leaves in place.
//
// A linter config is the one kind of gate that fails SILENTLY. `npx eslint .` exiting 0
// means either "the code is clean" or "the rules did not run", and nothing in the output
// distinguishes them. Every rule in that config is a regression guard for a SonarQube
// family this project already paid to clear, so a rule that quietly stops firing hands
// back the family it was added to protect while CI stays green — which is the exact
// failure #200 demonstrated, minus the linter.
//
// So each rule here is fed code that must trip it. This is the standing mutation-test
// discipline applied to config instead of production code: reintroduce the smell, prove
// the guard reddens.
//
// It has already earned its place three times over. Writing it caught (a)
// `prefer-specific-assertions` and `parameterized-tests` pinned in the MAIN block, where
// Sonar's own TEST scope means they can never fire; (b) `role="tablist"` used as a
// `prefer-tag-over-role` fixture, which that rule structurally cannot report because
// aria-query maps no tag to it; and (c) `http://evil.example.com` as a cleartext fixture,
// exempt under the rule's own DOCUMENTATION_HOSTS. All three read as passing.

import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  rmSync,
  statSync,
  utimesSync,
  writeFileSync,
} from "node:fs";
import os from "node:os";
import { execFileSync } from "node:child_process";
import path from "node:path";

import { ESLint } from "eslint";
import { beforeAll, describe, expect, test } from "vitest";

const FRONTEND_ROOT = import.meta.dirname;

/**
 * The plugin versions the SonarJS analyzer itself executes. RECORDED, and checked in BOTH
 * directions by the two tests at the bottom of this file — because drift has two sources
 * and guarding only one is worse than guarding neither, since it reads as covered:
 *
 *   npm moves ahead  — a Dependabot bump installs a newer plugin than the analyzer runs.
 *   SONAR moves ahead — `docker pull` lands a newer SonarQube whose analyzer bundles newer
 *                       plugins than these. Nothing on the npm side changes, so nothing in
 *                       a lockfile or a dependency PR ever mentions it.
 *
 * The second is the quiet one, and it was unguarded when these constants were introduced:
 * they were compared only against `node_modules`, so a Sonar upgrade left the gate mirroring
 * a bundle that no longer existed while every test stayed green.
 */
const ANALYZER_PLUGIN_VERSIONS: Record<string, string> = {
  "eslint-plugin-unicorn": "65.0.1",
  "eslint-plugin-react": "7.37.5",
  "eslint-plugin-jsx-a11y": "6.10.2",
  "eslint-plugin-testing-library": "7.16.2",
};

const SCANNER_CACHE = "/mnt/data/sonarqube/scanner-cache";

/**
 * The newest `sonar-javascript-plugin.jar` under `root` by mtime, or null when there is
 * none. The scanner cache keeps one entry per analyzer version it has ever downloaded, so
 * after a SonarQube upgrade it holds several (two since the 2026-09-11 move to 26.9). The
 * entry names are hashes and `readdirSync` orders by NAME, so "first jar found" is a coin
 * toss between the old bundle and the new one — and reading the old one is a silent pass
 * in exactly the direction the bundle test below exists to make loud.
 */
function newestAnalyzerJar(root: string): string | null {
  if (!existsSync(root)) return null;
  let newest: { jar: string; mtimeMs: number } | null = null;
  for (const entry of readdirSync(root)) {
    const jar = path.join(root, entry, "sonar-javascript-plugin.jar");
    if (!existsSync(jar)) continue;
    const { mtimeMs } = statSync(jar);
    if (newest === null || mtimeMs > newest.mtimeMs) newest = { jar, mtimeMs };
  }
  return newest?.jar ?? null;
}

/**
 * Re-derive the versions from the analyzer bundle actually on this machine, or return null
 * where it cannot be reached (CI runners, a fresh clone, a box with no scanner cache).
 * Null means UNKNOWN and the caller skips; it must never read as agreement.
 */
function analyzerBundleVersions(): Record<string, string> | null {
  const jar = newestAnalyzerJar(SCANNER_CACHE);
  if (jar === null) return null;
  try {
    // The jar embeds the analyzer tarball; the tarball's package.json is the manifest.
    const tgz = execFileSync("unzip", ["-p", jar, "sonarjs-*.tgz"], {
      maxBuffer: 256 * 1024 * 1024,
    });
    const manifest = execFileSync("tar", ["xzO", "package/package.json"], {
      input: tgz,
      maxBuffer: 64 * 1024 * 1024,
      encoding: "utf8",
    });
    const parsed = JSON.parse(manifest) as {
      dependencies?: Record<string, string>;
      devDependencies?: Record<string, string>;
    };
    return { ...parsed.dependencies, ...parsed.devDependencies };
  } catch {
    return null; // no unzip/tar, or an unreadable jar — unknown, not clean
  }
}

// Real, existing files: `lintText` supplies the CONTENT, but typescript-eslint's project
// service still resolves the PATH against the tsconfig to build type information, and the
// three type-aware rules here (`deprecation`, `prefer-read-only-props`,
// `prefer-optional-chain`) go dead without it. A path that does not exist fails as a
// parse error, which surfaces as "rule did not fire" — a false red, but an opaque one, so
// both files are asserted below.
const MAIN_FILE = path.join(FRONTEND_ROOT, "src/App.tsx");
const TEST_FILE = path.join(FRONTEND_ROOT, "src/App.test.tsx");

const eslint = new ESLint({ cwd: FRONTEND_ROOT });

async function rulesTrippedBy(code: string, filePath: string): Promise<string[]> {
  const [result] = await eslint.lintText(code, { filePath, warnIgnored: false });
  // An ignored path yields no result at all — neither messages nor a parse error — which
  // would pass every "must not trip" assertion for entirely the wrong reason.
  expect(result, `${filePath} produced no lint result — has it become ignored?`).toBeDefined();
  return (result?.messages ?? []).map((m) => m.ruleId ?? `PARSE ERROR: ${m.message}`);
}

/** Two similar helpers so `no-identical-functions` has something to compare. */
const SONARJS_TEST_PRELUDE = `import { describe, expect, it } from "vitest";
function stubRailTop(n: number) { return n; }
function measure() { return { maxHeight: "596px" }; }
`;

/**
 * One entry per rule enabled in `eslint.config.js`. Keep them in step: a rule added there
 * without a case here is an unproven guard, which is the state this file exists to
 * forbid. `enabledRulesAreAllCovered` below fails if they drift apart.
 */
const MUST_TRIP: ReadonlyArray<readonly [rule: string, filePath: string, code: string]> = [
  // --- MAIN scope ---------------------------------------------------------------
  [
    "@typescript-eslint/prefer-optional-chain",
    MAIN_FILE,
    `export const f = (a?: { b?: string }) => a && a.b && a.b.length;\n`,
  ],
  [
    "sonarjs/deprecation",
    MAIN_FILE,
    `/** @deprecated */ export function old() { return 1; }\nexport const x = old();\n`,
  ],
  [
    "sonarjs/prefer-read-only-props",
    MAIN_FILE,
    `export function C(props: { a: string }) { return <p>{props.a}</p>; }\n`,
  ],
  [
    "sonarjs/no-nested-conditional",
    MAIN_FILE,
    `export const f = (a: number, b: number) => (a ? (b ? 1 : 2) : 3);\n`,
  ],
  [
    "sonarjs/cognitive-complexity",
    MAIN_FILE,
    `export function f(a: number[], t: number) { let n = 0;
  for (const x of a) { if (x > t) { if (x % 2) { n++; } else if (x % 3) { n--; } else { n += 2; } }
  else if (x < 0) { if (x < -10) { n -= 2; } else { n--; } } else { while (n > 100) { n--; if (n === 50) break; } } }
  return n; }\n`,
  ],
  [
    "sonarjs/no-nested-template-literals",
    MAIN_FILE,
    "export const s = (a: string) => `x${`y${a}`}z`;\n",
  ],
  [
    // NOT `example.com`, `.test`, `localhost` or a loopback address: the rule exempts all
    // of those (SAFE_HOSTS and DOCUMENTATION_HOSTS in its own source), and this project's
    // only two historical hits were bare `http://plex:PORT` hostnames.
    "sonarjs/no-clear-text-protocols",
    MAIN_FILE,
    `export const u = "http://plex:9999/x";\n`,
  ],
  ["sonarjs/prefer-regexp-exec", MAIN_FILE, `export const m = (s: string) => s.match(/abc/);\n`],
  [
    "sonarjs/reduce-initial-value",
    MAIN_FILE,
    `export const t = (a: number[]) => a.reduce((x, y) => x + y);\n`,
  ],
  [
    // A STRING array on purpose: that is the shape the family was raised on, and it is
    // the shape `@typescript-eslint/require-array-sort-compare` — the twin this gate
    // deliberately does not use — ignores by default. What this case pins is that
    // SonarJS's own rule keeps reporting that shape; a plugin upgrade that gave it the
    // same default exemption would leave the gate listed, green and inert. It cannot
    // catch a SUBSTITUTED rule — a rule key names its plugin, so swapping one changes the
    // key and the enabled-vs-covered test below fails on the key instead.
    "sonarjs/no-alphabetical-sort",
    MAIN_FILE,
    `export const s = (a: string[]) => [...a].sort();\n`,
  ],
  [
    // Needs bodies of at least DEFAULT_MIN_LINES (3) lines, so these cannot be collapsed.
    "sonarjs/no-identical-functions",
    MAIN_FILE,
    `export function a(n: number) {
  const q = n * 2;
  const r = q + 1;
  const s = r * 3;
  return s - 1;
}
export function b(n: number) {
  const q = n * 2;
  const r = q + 1;
  const s = r * 3;
  return s - 1;
}
`,
  ],
  [
    // `role="navigation"` maps to `<nav>`. A role with no tag mapping in aria-query
    // (`tablist`, for one) can never trip this rule, however wrong the markup looks.
    "jsx-a11y/prefer-tag-over-role",
    MAIN_FILE,
    `export const A = () => <div role="navigation" />;\n`,
  ],
  [
    // `noInlineConfig` pin. Without it an `eslint-disable` comment silences this gate while
    // SonarQube keeps reporting the family — its suppression channel is `NOSONAR` or a
    // server-side resolution, not an ESLint comment — which is the #200 failure exactly:
    // CI green, family regrown. Both comment forms are here because they are separate
    // ESLint features and `noInlineConfig` is what disables each.
    "sonarjs/no-nested-conditional",
    MAIN_FILE,
    `/* eslint-disable sonarjs/no-nested-conditional */
// eslint-disable-next-line sonarjs/no-nested-conditional
export const f = (a: number, b: number) => (a ? (b ? 1 : 2) : 3);\n`,
  ],
  [
    // THE mutation case for the decorator, and the reason it exists. The exemption is the
    // `status` + `aria-live` PAIR; a bare `role="status"` must still fail. Without this
    // line, widening the exemption to every `role="status"` in the codebase leaves all
    // other cases green — the decorator is the one piece of hand-written logic in the
    // config, so its NARROWING needs a case as much as its widening does.
    "jsx-a11y/prefer-tag-over-role",
    MAIN_FILE,
    `export const A = () => <p role="status">x</p>;\n`,
  ],
  [
    "jsx-a11y/no-static-element-interactions",
    MAIN_FILE,
    `export const A = () => <div onClick={() => {}} />;\n`,
  ],
  [
    "jsx-a11y/mouse-events-have-key-events",
    MAIN_FILE,
    `export const A = () => <div onMouseOver={() => {}} />;\n`,
  ],
  [
    // The other half of S1082, which Sonar merges into one rule. `<li onClick>` escaped the
    // gate entirely until this was added — and S6848 does not cover it, because that rule
    // exempts anything with a role, implicit included, which `li` has.
    "jsx-a11y/click-events-have-key-events",
    MAIN_FILE,
    `export const A = () => <li onClick={() => {}} />;\n`,
  ],
  [
    "jsx-a11y/img-redundant-alt",
    MAIN_FILE,
    `export const A = () => <img src="x.png" alt="picture of a cat" />;\n`,
  ],
  ["jsx-a11y/no-noninteractive-tabindex", MAIN_FILE, `export const A = () => <div tabIndex={0} />;\n`],
  [
    // The fixture must contain a REAL escaped backslash once the template literal is
    // resolved — `"a\\b"` in the linted source, not `"a\b"`. Written with `\\\\` here for
    // exactly that reason; halving it produces a `\b` escape, which the rule ignores.
    "unicorn/prefer-string-raw",
    MAIN_FILE,
    `export const s = "a\\\\b";\n`,
  ],
  [
    // The array must be `const` and used ONLY for membership tests: the rule is about an
    // array that should have been a Set, so any other use of `known` makes it silent.
    "unicorn/prefer-set-has",
    MAIN_FILE,
    `const known = ["a", "b", "c"];\nexport const f = (x: string) => known.includes(x);\n`,
  ],
  [
    "unicorn/prefer-default-parameters",
    MAIN_FILE,
    `export function f(a?: number) { a = a ?? 1; return a; }\n`,
  ],
  [
    // Two inline elements separated by nothing but a newline — the whitespace HTML would
    // render and JSX silently drops. It has to be a literal line break inside the element.
    "react/jsx-child-element-spacing",
    MAIN_FILE,
    `export const A = () => <p><a href="#">x</a>\ny</p>;\n`,
  ],
  [
    "react/jsx-no-constructed-context-values",
    MAIN_FILE,
    `import { createContext } from "react";
const C = createContext<{ a: number } | null>(null);
export const A = () => <C.Provider value={{ a: 1 }}><i /></C.Provider>;\n`,
  ],
  [
    "react/no-unstable-nested-components",
    MAIN_FILE,
    `export const A = () => { const B = () => <i />; return <div><B /></div>; };\n`,
  ],
  // S1186 has three reported SHAPES and the decorator recognises them one at a time, so
  // each gets its own case: deleting any single arm of `isSonarReportedShape` must redden
  // something. A single fixture would leave two arms free to be dropped in silence.
  ["sonar-mirror/no-empty-function", MAIN_FILE, `export function doNothing() {}\n`],
  ["sonar-mirror/no-empty-function", MAIN_FILE, `export class K { run() {} }\n`],
  ["sonar-mirror/no-empty-function", MAIN_FILE, `export const handler = function () {};\n`],

  // --- The self-lint block (scoped to `eslint.config.js` itself) ---------------
  // No constant for this path on purpose: the rule is scoped to that one file, and the
  // literal path keeps the entry honest — a fixture pointed at MAIN_FILE would trip the
  // MAIN block's copy of the same rule and pass while the self-lint block goes inert
  // (e.g. `allowJs` dropped from `tsconfig.node.json`). The deprecated symbol must be
  // resolvable through the project, so the fixture imports the real package; the known
  // deprecated call is `tseslint.config` (the very shape #200's S1874 escaped as).
  [
    "sonarjs/deprecation",
    "eslint.config.js",
    `import tseslint from "typescript-eslint";\nexport const c = tseslint.config({});\n`,
  ],

  // --- TEST scope ---------------------------------------------------------------
  [
    "testing-library/prefer-find-by",
    TEST_FILE,
    `import { waitFor, screen } from "@testing-library/react";
export const t = async () => { await waitFor(() => screen.getByText("a")); };\n`,
  ],
  [
    "sonarjs/prefer-specific-assertions",
    TEST_FILE,
    `import { expect, test } from "vitest";
test("x", () => { expect([1].length).toBe(0); });\n`,
  ],
  [
    // Three near-identical tests of THREE statements each. Two statements is not enough
    // in practice, despite the rule's `MIN_STATEMENTS = 2`; this shape was reduced from
    // the real pre-fix `useRailMaxHeight.test.tsx` that Sonar flagged, rather than
    // written from the constant.
    "sonarjs/parameterized-tests",
    TEST_FILE,
    `${SONARJS_TEST_PRELUDE}describe("g", () => {
  it("a", () => { stubRailTop(180); measure(); expect(measure().maxHeight).toBe("596px"); });
  it("b", () => { stubRailTop(60); measure(); expect(measure().maxHeight).toBe("680px"); });
  it("c", () => { stubRailTop(96); measure(); expect(measure().maxHeight).toBe("704px"); });
});
`,
  ],
];

/**
 * The other half of the gate. A rule that fires on everything is as useless as one that
 * fires on nothing — it just fails loudly instead of quietly, and the fix is to delete
 * it. These pin the three deliberate narrowings in the config.
 */
const MUST_NOT_TRIP: ReadonlyArray<
  readonly [label: string, ruleFragment: string, filePath: string, code: string]
> = [
  // The four `prefer-tag-over-role` cases below are not four opinions — each mirrors one
  // line of SonarJS's own decorator, read out of the analyzer bundle (the path is in
  // `eslint.config.js`). Change one only with the corresponding minified predicate open.
  [
    "role=status with aria-live is exempt (SonarJS U8g)",
    "prefer-tag-over-role",
    MAIN_FILE,
    `export const B = () => <p role="status" aria-live="polite">x</p>;\n`,
  ],
  [
    // `U8g` is `role==="status" && !!getProp(attrs,"aria-live")` — PRESENCE, not value.
    // `aria-live="off"` is not a live region and an a11y linter would rightly flag it,
    // but this gate mirrors Sonar; being stricter here fails the build on code the
    // server accepts. An earlier version allowlisted polite/assertive and broke that.
    'aria-live="off" is exempt too, because U8g tests presence not value',
    "prefer-tag-over-role",
    MAIN_FILE,
    `export const B = () => <p role="status" aria-live="off">x</p>;\n`,
  ],
  [
    // Pins the expression-container unwrap in `getLiteralPropValue`. It must be
    // `{"status"}` lowercase: the base rule looks roles up in a lowercase-keyed table and
    // returns early on `{"STATUS"}`, so an uppercase fixture passes with the decorator
    // deleted entirely and proves nothing. Verified against the raw rule — it fires on
    // `role={"status"}` and is silent on `role={"STATUS"}` — so this case has an oracle.
    'role={"status"} resolves like the bare literal',
    "prefer-tag-over-role",
    MAIN_FILE,
    `export const B = () => <p role={"status"} aria-live="polite">x</p>;\n`,
  ],
  [
    // Sonar's `Qdr` admits only native HTML tag names, so a custom component is never
    // reported however it is roled. This is the likeliest live over-fire in a shadcn
    // codebase full of wrappers — `<Alert role="status">` is ordinary code.
    "a custom component is never reported (SonarJS Qdr admits native tags only)",
    "prefer-tag-over-role",
    MAIN_FILE,
    `function Alert(props: Readonly<{ role?: string }>) { return <div className={props.role} />; }
export const B = () => <Alert role="navigation" />;\n`,
  ],
  [
    // `nlm` contains `svg` and `math` but NO SVG child elements, so these are silent at
    // Sonar. A `/^[a-z]/` test — the obvious way to write `Qdr` — reddens all of them, and
    // `Logo.tsx` already contains `<g>`, `<path>` and `<rect>`.
    "SVG child elements are not native HTML tags, so they are never reported",
    "prefer-tag-over-role",
    MAIN_FILE,
    `export const B = () => <svg><g role="navigation" /><circle role="button" /></svg>;\n`,
  ],
  [
    "a web component is not a native HTML tag either",
    "prefer-tag-over-role",
    MAIN_FILE,
    `export const B = () => <my-el role="navigation" />;\n`,
  ],
  [
    // `getProp` defaults to `ignoreCase: true` and `getLiteralPropValue` resolves template
    // literals. Hand-rolled equivalents missed both and over-fired.
    "getProp/getLiteralPropValue handle ROLE= and template literals",
    "prefer-tag-over-role",
    MAIN_FILE,
    "export const B = () => <p ROLE={`status`} aria-live=\"polite\">x</p>;\n",
  ],
  // The six `no-empty-function` cases below split into two groups that fail for different
  // reasons, and both groups need pinning. The first four are the DECORATOR
  // (`reportOnlyNamedEmptyFunctionShapes`); the last two are Sonar's `allow` OPTIONS. Every
  // one was checked against the undecorated, unoptioned rule first — all six report there,
  // so none of these passes vacuously.
  [
    // `lCn`: an `Identifier` matching `/noop/i` anywhere in the name.
    "an empty function named noop is exempt (SonarJS lCn)",
    "no-empty-function",
    MAIN_FILE,
    `export function noop() {}\n`,
  ],
  [
    // `lCn`'s other half, `/^on[A-Z]/` — anchored and case-sensitive.
    "an empty function named onSelect is exempt (SonarJS lCn)",
    "no-empty-function",
    MAIN_FILE,
    `export function onSelect() {}\n`,
  ],
  [
    // `_Mf` arm 2 is `MethodDefinition`, which an object literal is not — its function sits
    // under a `Property`. Sonar is silent; the raw rule calls it an empty "method".
    "an empty object-literal method is exempt (not a MethodDefinition)",
    "no-empty-function",
    MAIN_FILE,
    `export const o = { m() {} };\n`,
  ],
  [
    // The shape that makes this decorator worth having: an empty function passed straight
    // to a call is none of `_Mf`'s three arms, and it is what every test double looks like.
    "an empty callback argument is exempt (none of _Mf's three shapes)",
    "no-empty-function",
    MAIN_FILE,
    `declare function run(cb: () => void): void;\nrun(function () {});\n`,
  ],
  [
    'Sonar\'s allow list covers arrow functions',
    "no-empty-function",
    MAIN_FILE,
    `export const a = () => {};\n`,
  ],
  [
    'Sonar\'s allow list covers constructors',
    "no-empty-function",
    MAIN_FILE,
    `export class K { constructor() {} }\n`,
  ],
  // Two react-intl arms of S6478's `RBg`, plus the widened `propNamePattern`. MusicDrop
  // uses none of these today; they pin the mirror, not the codebase.
  [
    "a react-intl `values` render prop is exempt (SonarJS OBg)",
    "no-unstable-nested-components",
    MAIN_FILE,
    `declare const FormattedMessage: (p: { id: string; values: Record<string, unknown> }) => null;
export const A = () => <FormattedMessage id="x" values={{ b: (c: string) => <b>{c}</b> }} />;\n`,
  ],
  [
    "the second argument of formatMessage is exempt (SonarJS FBg)",
    "no-unstable-nested-components",
    MAIN_FILE,
    `declare const intl: { formatMessage: (d: object, v: object) => string };
export const A = () => { const s = intl.formatMessage({ id: "x" }, { b: (c: string) => <b>{c}</b> }); return <p>{s}</p>; };\n`,
  ],
  [
    // Sonar's `propNamePattern` default is `{render*,*Enhancer,*Renderer}`; the plugin's own
    // is the narrower `render*`, under which `itemRenderer` reports. Dropping the option
    // object reddens this and nothing else.
    "a *Renderer prop counts as a render prop under Sonar's wider propNamePattern",
    "no-unstable-nested-components",
    MAIN_FILE,
    `declare const Grid: (p: { itemRenderer: () => unknown }) => null;
export const A = () => <Grid itemRenderer={() => <i />} />;\n`,
  ],
  [
    "a MAIN-scope rule stays off in test files",
    "no-clear-text-protocols",
    TEST_FILE,
    `export const u = "http://plex:9999/x";\n`,
  ],
  [
    "a TEST-scope rule stays off in source files",
    "prefer-find-by",
    MAIN_FILE,
    `import { waitFor, screen } from "@testing-library/react";
export const t = async () => { await waitFor(() => screen.getByText("a")); };\n`,
  ],
];

describe("eslint.config.js", () => {
  beforeAll(() => {
    for (const f of [MAIN_FILE, TEST_FILE]) {
      expect(existsSync(f), `fixture anchor ${f} no longer exists — pick another real file`).toBe(
        true,
      );
    }
  });

  // `#%#` disambiguates: prefer-tag-over-role has three cases, one per boundary of its
  // exemption, so the names would otherwise collide.
  test.each(MUST_TRIP)("[#%#] %s fires on the smell it guards", async (rule, filePath, code) => {
    expect(await rulesTrippedBy(code, filePath)).toContain(rule);
  }, 30_000);

  test.each(MUST_NOT_TRIP)("%s", async (_label, ruleFragment, filePath, code) => {
    const tripped = await rulesTrippedBy(code, filePath);
    expect(tripped.filter((r) => r.includes(ruleFragment))).toEqual([]);
    // A parse error would empty the list above and pass the assertion vacuously.
    expect(tripped.filter((r) => r.startsWith("PARSE ERROR"))).toEqual([]);
  }, 30_000);

  test(
    "every enabled rule has a mutation case, and is an error not a warning",
    async () => {
      const config = await eslint.calculateConfigForFile(MAIN_FILE);
      const testConfig = await eslint.calculateConfigForFile(TEST_FILE);
      const enabled = new Set(
        [...Object.entries(config.rules ?? {}), ...Object.entries(testConfig.rules ?? {})]
          .filter(([, entry]) => {
            const severity = Array.isArray(entry) ? entry[0] : entry;
            return severity !== 0 && severity !== "off";
          })
          .map(([name, entry]) => {
            // `eslint .` exits 0 when only warnings are present, so a single "error" ->
            // "warn" edit disables the gate while every other test here stays green: the
            // smell is still REPORTED, so `toContain(rule)` passes, and "is it enabled"
            // accepts warn. `--max-warnings 0` in the npm script closes the same hole from
            // the other side; both are kept, because they fail at different moments —
            // this one the instant the config is edited, that one only once a violation
            // actually exists.
            const severity = Array.isArray(entry) ? entry[0] : entry;
            // `calculateConfigForFile` returns the NORMALISED severity, so "error" comes
            // back as 2 and "warn" as 1 whichever spelling the config used.
            const normalised = severity === "error" ? 2 : severity === "warn" ? 1 : severity;
            expect(
              normalised,
              `${name} must be "error" (2), not "warn" — \`eslint .\` exits 0 on warnings`,
            ).toBe(2);
            return name;
          }),
      );
      const covered = new Set(MUST_TRIP.map(([rule]) => rule));
      expect([...enabled].filter((r) => !covered.has(r)).sort()).toEqual([]);
      expect([...covered].filter((r) => !enabled.has(r)).sort()).toEqual([]);
    },
    30_000,
  );

  test(
    "every source file on disk is actually reached by a rule block",
    async () => {
      // Enumerated, NOT a hard-coded list. Both fixture anchors live at `src/App.tsx` and
      // `src/App.test.tsx`, so a spot-check list only guards the directories it happens to
      // name: adding `src/components/settings/**` to `ignores` left all other cases green
      // while 15 MAIN rules stopped covering that directory, and planting six real smells
      // there still gave `npm run lint` exit 0. The ESLint file count is not a signal
      // either — the parsing-only block keeps visiting ignored files, it just applies no
      // rules to them.
      // `ui/**` is in `sonar.exclusions` (sonar-project.properties:22), which that file's
      // own header says filters the SOURCE scope ONLY — `sonar.test.exclusions` (line 19)
      // does not list it. So Sonar DOES analyse the `*.test.tsx` files under `ui/`, and
      // SONAR_TEST does lint them. Skipping the directory wholesale hid three of them
      // (`card`, `popover`, `segmented-control`) from this assertion: adding
      // `src/components/ui/**` to SONAR_TEST's `ignores` left all 53 tests green while
      // those files stopped being reached by every TEST rule — `prefer-find-by` (S9020)
      // among them, one of the three families #200 regrew. Keep the tests, drop the rest.
      const walk = (dir: string): string[] =>
        readdirSync(dir, { withFileTypes: true }).flatMap((e) => {
          const full = path.join(dir, e.name);
          if (e.isDirectory()) {
            return e.name === "ui"
              ? walk(full).filter((f) => /\.test\.tsx?$/.test(f))
              : walk(full);
          }
          return /\.tsx?$/.test(e.name) && !full.endsWith(".d.ts") ? [full] : [];
        });

      const sources = [
        ...walk(path.join(FRONTEND_ROOT, "src")),
        path.join(FRONTEND_ROOT, "vite.config.ts"),
        path.join(FRONTEND_ROOT, "vitest.config.ts"),
        path.join(FRONTEND_ROOT, "eslint.config.test.ts"),
        path.join(FRONTEND_ROOT, "eslint.config.js"),
      ];
      expect(sources.length).toBeGreaterThan(100); // the walk itself must not silently empty

      const unreached: string[] = [];
      for (const file of sources) {
        const config = await eslint.calculateConfigForFile(file);
        if (Object.keys(config.rules ?? {}).length === 0) {
          unreached.push(path.relative(FRONTEND_ROOT, file));
        }
      }
      expect(unreached, "these files are linted by no rule block").toEqual([]);
    },
    60_000,
  );

  test(
    "the react version setting is the analyzer's sentinel, not the installed react",
    async () => {
      const config = await eslint.calculateConfigForFile(MAIN_FILE);
      const settings = config.settings as { react?: { version?: string } };
      // `999.999.999` is `ULTIMATE_LATEST_SEMVER` in `eslint-plugin-react`, and it is what
      // SonarJS writes into every file's config (`settings:{react:{version:"999.999.999"}}`
      // in the analyzer bundle). Two wrong-looking-right edits are what this pins.
      //
      // `"detect"` is the first. It CRASHES on ESLint 10 — `resolveBasedir` calls the
      // removed `context.getFilename()` — taking down every rule that routes through
      // `Components.detect`, which here is `no-unstable-nested-components` and
      // `jsx-no-constructed-context-values`. Their MUST_TRIP cases above would catch that
      // one, loudly.
      //
      // Pinning the INSTALLED react version is the second, and NOTHING else here would
      // catch it: the rules keep loading and keep firing, they just stop agreeing with the
      // server on anything version-sensitive. So the assertion below is deliberately
      // inverted from the obvious "keep these two in sync" test — they must NOT match, and
      // a future sync is the bug, not the fix.
      expect(settings.react?.version).toBe("999.999.999");
      const pkg = JSON.parse(readFileSync(path.join(FRONTEND_ROOT, "package.json"), "utf8")) as {
        dependencies: Record<string, string>;
      };
      expect(pkg.dependencies.react).toBeTruthy();
      expect(pkg.dependencies.react.replace(/^[^\d]*/, "")).not.toBe(settings.react?.version);
    },
    30_000,
  );

  test(
    "the testing-library aggressive-reporting settings are all off, as SonarJS injects them",
    async () => {
      const config = await eslint.calculateConfigForFile(TEST_FILE);
      const settings = config.settings as Record<string, unknown>;
      // Transcribed from `yAh` in the analyzer bundle. All three, or the mirror is partial:
      // each switches off a different half of Aggressive Reporting (modules, renders,
      // queries), and leaving one on lets the gate report a custom wrapper the server
      // never looks at.
      expect(settings["testing-library/utils-module"]).toBe("off");
      expect(settings["testing-library/custom-renders"]).toBe("off");
      expect(settings["testing-library/custom-queries"]).toBe("off");
      // And they must NOT leak into the MAIN block: `settings` in flat config is per-block,
      // and a stray copy there would be a silent claim that a TEST-scope rule runs on
      // source files.
      // `?? {}` on purpose: without it this throws a TypeError rather than failing an
      // assertion whenever the MAIN block carries no `settings` at all, which makes the
      // diagnostic opaque AND makes the pass quietly depend on the react sentinel keeping
      // `settings` non-empty. The assertion should hold on its own terms either way.
      const mainConfig = await eslint.calculateConfigForFile(MAIN_FILE);
      expect(
        ((mainConfig.settings ?? {}) as Record<string, unknown>)["testing-library/utils-module"],
      ).toBeUndefined();
    },
    30_000,
  );

  test("the npm script runs the gate the way CI needs it to", () => {
    // Nothing else here reads `package.json`. Narrowing `eslint .` to `eslint src` would
    // drop `vite.config.ts`, `vitest.config.ts` and this file from CI while every
    // `calculateConfigForFile` assertion above stayed green, because those consult the
    // config, never the command.
    const pkg = JSON.parse(readFileSync(path.join(FRONTEND_ROOT, "package.json"), "utf8")) as {
      scripts: Record<string, string>;
    };
    expect(pkg.scripts.lint).toContain("--max-warnings 0");
    expect(pkg.scripts.lint).toMatch(/eslint\s+\.(\s|$)/);
  });

  test("the mirrored plugins are still at the exact versions the analyzer runs", () => {
    // Five of the enabled rules are justified as "raw is faithful" — the claim that a
    // `decorated` Sonar rule reports the same set as the bare ESLint rule. Every one of
    // those claims was read out of ONE build of the analyzer bundle and holds only at the
    // versions that build ships. `package.json` carries caret ranges, so a routine minor
    // satisfies them; nothing else in this file would notice.
    //
    // The concrete failing scenario, and the reason this is an assertion rather than a
    // comment: `propNamePattern` — the option the S6478 mirror depends on — was itself
    // added to `no-unstable-nested-components` in a 7.3x MINOR, and the plugin's own
    // default is the narrower `render*`. So a minor CAN move a mirrored rule. Ship
    // `eslint-plugin-react@7.38.0` with a widened `jsx-child-element-spacing`, let
    // Dependabot land it, and the gate starts reporting code the analyzer (still on
    // 7.37.5) accepts — the one property this config must never have — with all other
    // tests green.
    //
    // Re-derive when this fails, do NOT just bump the constant. The bundle declares them:
    //   unzip -p /mnt/data/sonarqube/scanner-cache/*/sonar-javascript-plugin.jar sonarjs-*.tgz \
    //     | tar xzO package/package.json | python3 -m json.tool
    // A failure means the gate and the server have drifted apart: read the new bundle's
    // rule bodies, confirm each "raw is faithful" claim still holds, THEN update both.
    for (const [name, expected] of Object.entries(ANALYZER_PLUGIN_VERSIONS)) {
      const installed = (
        JSON.parse(
          readFileSync(path.join(FRONTEND_ROOT, "node_modules", name, "package.json"), "utf8"),
        ) as { version: string }
      ).version;
      expect(
        installed,
        `${name} is ${installed}, the analyzer runs ${expected} — re-read the bundle before changing this`,
      ).toBe(expected);
    }
  });

  test("the bundle read is the NEWEST jar in the scanner cache, not the first by name", () => {
    // Entry names are hashes, so name order is a coin toss between the old bundle and the
    // new one; only the newest describes the analyzer the server runs now. Built in a temp
    // dir so the real cache is never touched.
    const root = mkdtempSync(path.join(os.tmpdir(), "scanner-cache-"));
    try {
      const older = path.join(root, "a-sorts-first", "sonar-javascript-plugin.jar");
      const newer = path.join(root, "z-sorts-last", "sonar-javascript-plugin.jar");
      for (const jar of [older, newer]) {
        mkdirSync(path.dirname(jar));
        writeFileSync(jar, "");
      }
      const now = Date.now() / 1000;
      utimesSync(older, now - 3600, now - 3600);
      utimesSync(newer, now, now);
      expect(newestAnalyzerJar(root)).toBe(newer);
      // The other way round too, so this is mtime and not "last by name" in disguise.
      utimesSync(older, now + 60, now + 60);
      expect(newestAnalyzerJar(root)).toBe(older);
      // An entry with no jar is skipped; a missing cache is unknown, not clean.
      mkdirSync(path.join(root, "m-no-jar"));
      expect(newestAnalyzerJar(root)).toBe(older);
      expect(newestAnalyzerJar(path.join(root, "missing"))).toBeNull();
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });

  test("the recorded analyzer versions still match the analyzer bundle on disk", () => {
    // The OTHER direction of drift, and the one nothing else can see. The test above only
    // proves `node_modules` agrees with the constants above; it says nothing about whether
    // those constants still describe the analyzer. Upgrade the SonarQube container and the
    // bundled plugins move on their own — no lockfile change, no dependency PR, nothing for
    // Dependabot to report — and the gate quietly stops mirroring the server while every
    // other test here stays green. That is the failure this test exists to make loud.
    //
    // It is LOCAL-ONLY by necessity: the scanner cache is a machine path, so CI cannot see
    // it. Skipping is honest (the answer is unknown there); passing would not be. The event
    // it guards — pulling a new SonarQube image — happens on this machine anyway, so the
    // check fires where the change actually lands.
    //
    // Re-derive by hand with:
    //   unzip -p "$(ls -t /mnt/data/sonarqube/scanner-cache/*/sonar-javascript-plugin.jar | head -1)" 'sonarjs-*.tgz' \
    //     | tar xzO package/package.json | python3 -m json.tool
    //
    // When this fails, do NOT just update the constants. The versions moving means the rules
    // may have moved: re-read each "raw is faithful" claim in eslint.config.js against the
    // NEW bundle, then update the constants and `package.json` together.
    const bundle = analyzerBundleVersions();
    if (bundle === null) {
      console.warn(
        "[analyzer pin] bundle not reachable on this machine — recorded versions UNVERIFIED against it",
      );
      return;
    }

    for (const [name, recorded] of Object.entries(ANALYZER_PLUGIN_VERSIONS)) {
      expect(
        bundle[name],
        `the analyzer bundle now runs ${name}@${bundle[name]}, but this file records ${recorded} — SonarQube was upgraded; re-verify the mirrored rules before bumping`,
      ).toBe(recorded);
    }
  });
});
