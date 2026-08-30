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

import { existsSync, readdirSync, readFileSync } from "node:fs";
import path from "node:path";

import { ESLint } from "eslint";
import { beforeAll, describe, expect, test } from "vitest";

const FRONTEND_ROOT = import.meta.dirname;

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
      const walk = (dir: string): string[] =>
        readdirSync(dir, { withFileTypes: true }).flatMap((e) => {
          const full = path.join(dir, e.name);
          if (e.isDirectory()) return e.name === "ui" ? [] : walk(full); // ui/** = sonar.exclusions
          return /\.tsx?$/.test(e.name) && !full.endsWith(".d.ts") ? [full] : [];
        });

      const sources = [
        ...walk(path.join(FRONTEND_ROOT, "src")),
        path.join(FRONTEND_ROOT, "vite.config.ts"),
        path.join(FRONTEND_ROOT, "vitest.config.ts"),
        path.join(FRONTEND_ROOT, "eslint.config.test.ts"),
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
});
