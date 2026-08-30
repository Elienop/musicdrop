// ESLint, scoped deliberately narrow: this exists to PIN the SonarQube families the
// 2026-08-25/26 remediation programme drove to zero, not to impose a new house style.
//
// Why it exists at all: the programme's standing "lock-on-clear" rule says the PR that
// drives a family to zero also enables its lint twin, so CI holds the line. The backend
// honours that through ruff `select`. The frontend never could — there was no linter here
// at all — and the cost stopped being hypothetical in auth slice 2 (#200), which
// reintroduced THREE already-cleared families in new code (S9020, S6819, S1874), caught
// only by the server-side scan after the code was written, reviewed three times, and
// browser-verified.
//
// The selection rule, and the reason there is no `recommended` preset below: every rule
// here maps to a Sonar rule this project has actually violated and fixed — 19 rules
// covering 18 of the 27 TypeScript families (768 resolved issues) that the programme
// cleared, the counts differing because S1082 is a union of two ESLint rules — and `main`
// currently sits at 0 open issues, so each one is a regression guard rather than a new
// opinion. Turning on a preset would flag code nobody has agreed to change and would make
// the gate impossible to land.
//
// The pragmatic case for an allowlist is that extending the presets produced 182 findings,
// 30 of them from rules nobody selected. The structural case is stronger and is the reason
// not to revisit this: a preset makes the gate's contents a function of somebody else's
// release cadence, so "every rule maps to a family we cleared" stops being checkable on the
// next `npm update` — and `eslint.config.test.ts` could not be written at all, because
// there would be no finite set to enumerate. The allowlist is what makes the gate testable.
//
// Each rule below carries its Sonar id and the number of issues that family cost, so a
// future reader can judge whether it still earns its place. Every id, count and mapping
// was read back from the running server (`api/rules/show`, `api/issues/search` against
// projectKey `musicdrop`) rather than from memory — one mapping was wrong when written
// from memory, see the S6848 note.
//
// A MAPPING IS NOT ALWAYS ONE-TO-ONE, which is the subtlety that has cost the most here.
// Every SonarJS registration carries an `implementation` of `original`, `external` or
// `decorated`, plus an `externalRules` array — all readable in the same bundle the
// decorator note below cites. Four of the rules enabled here are twins of `decorated`
// Sonar rules, and `decorated` means the ESLint rule is wrapped, merged, or both:
//
//   S6819  prefer-tag-over-role     decorated  -> wrapped below, faithfully
//   S1082  mouse-events-a11y        decorated  -> a UNION of two rules; both now enabled
//   S6582  prefer-optional-chain    decorated  -> run RAW; Sonar adds six suppressions
//   S9020  prefer-find-by           decorated  -> run RAW; Sonar suppresses on type
//
// The last two are knowingly unmirrored. Sonar's S6582 stays silent when the contextual
// type of the whole logical chain excludes `undefined | any | unknown | void`, and S9020
// when the queried receiver is not a testing-library type — so both can red the build on
// code the server accepts. Neither does so in the tree today, and mirroring them means
// porting type-directed predicates, which is a different order of work from the four-line
// mirror below. Recorded rather than done: if either fires on something Sonar is silent
// about, this comment is the place to start, not a disable comment.

// `defineConfig`/`globalIgnores` come from ESLint core, not from `tseslint.config`.
// typescript-eslint deprecated its own helper once core shipped the same functionality
// (`config-helper.d.ts:67`), and the deprecated signature is a `javascript:S1874` — the
// very family #200 regrew. Caught by the server scan, because this file is one of the few
// the gate below cannot lint: every block needs the typed parser, and no tsconfig includes
// a `.js` file at the frontend root.
import { defineConfig, globalIgnores } from "eslint/config";
import tseslint from "typescript-eslint";
import sonarjs from "eslint-plugin-sonarjs";
import a11y from "eslint-plugin-jsx-a11y";
import testingLibrary from "eslint-plugin-testing-library";
// Both are declared as direct devDependencies even though `eslint-plugin-jsx-a11y` already
// pulls them in: this file imports them, so it owns the declaration. npm dedupes them onto
// the same copy the plugin uses, which is the point — a mirror that read a different
// `aria-query` than the rule it wraps would drift silently.
import { dom } from "aria-query";
// `jsx-ast-utils` is CommonJS, so a named ESM import throws at config-load time
// ("Named export 'getLiteralPropValue' not found"). Destructure the default, which is what
// `eslint-plugin-jsx-a11y` does internally for the same reason.
import jsxAstUtils from "jsx-ast-utils";

const { getProp, getLiteralPropValue } = jsxAstUtils;

// --- Matching Sonar's own MAIN/TEST split ------------------------------------
//
// Sonar rules carry a `scope` in their metadata, and the scanner only raises a
// MAIN-scope rule against a file it qualifies as source. `sonar-project.properties`
// puts `**/*.test.ts`/`**/*.test.tsx` in `sonar.test.inclusions` (line 16), so those
// files are UTS-qualified and a MAIN rule structurally CANNOT fire in them; it also
// lists `frontend/src/components/ui/**` in `sonar.exclusions` (line 22), the vendored
// shadcn primitives nobody here hand-edits.
//
// Mirroring that split is what keeps this config honest. Without it the gate fails on
// 19 findings that Sonar is not merely tolerating but is incapable of raising — 15
// `no-clear-text-protocols` on the `http://plex:PORT` and `http://slskd:PORT` fixtures in
// the settings-panel tests, and 4 `prefer-read-only-props` on test-local prop types — and
// a gate that reports what the thing it mirrors cannot report is a different gate wearing
// its name. (Not localhost: the rule's own SAFE_HOSTS exempts loopback, which is why
// production code is clean on both sides.)
//
// Scopes were read per rule from `api/rules/show`, not guessed: of the 19 enabled below,
// 16 are MAIN and THREE are TEST — S9020, S5906 and S5976. That is not deducible from the
// names, which is the whole reason the split is per-rule; `no-clear-text-protocols` fires
// only on test fixtures here and is still MAIN.
// Both globs are `**`, matching `sonar.sources=.`, and the narrowing is expressed ONLY as
// `ignores`. An earlier version listed `src/**` plus `vite.config.ts` and `vitest.config.ts`
// by name, which is the same shape as an allowlist of directories and fails the same way:
// four kinds of file Sonar scans went unlinted — a new `scripts/` directory, a root-level
// `.ts`, a root-level `*.test.ts`, and a `.js` under `src/` — each one silent, because a
// file matching no `files` pattern is not an error, it simply has no rules. Enumerating
// what to lint means every new location starts outside the gate; enumerating what to skip
// means it starts inside. The second is the only one that fails safe.
//
// `src/test/**` (the vitest harness) is deliberately NOT excluded: `sonar.test.inclusions`
// does not match it, so Sonar treats it as source and so does this.
const SONAR_MAIN = {
  files: ["**/*.{ts,tsx}"],
  ignores: ["**/*.test.{ts,tsx}", "src/components/ui/**"],
};
// Exactly `sonar.test.inclusions` (`**/*.test.ts`, `**/*.test.tsx`), at any depth — which
// is why `eslint.config.test.ts` at the frontend root needs no special-casing.
const SONAR_TEST = { files: ["**/*.test.{ts,tsx}"] };

// --- The one rule that is wrapped rather than used raw -----------------------
//
// `jsx-a11y/prefer-tag-over-role` is S6819's twin, but Sonar accepts a `role="status"`
// that is also a live region, and the raw rule does not. Seven such sites exist here.
//
// The exemption is measured, not assumed. All seven lines were last modified between
// 2026-05-27 and 2026-08-21; the last `main` analysis ran 2026-08-30 and reports 0 open
// S6819. Sonar scanned this exact code and stayed silent, so those seven findings are
// OVER-FIRES against the family being pinned, not misses by Sonar.
//
// Deleting the rule was the obvious response and is the wrong one: S6819 cost 45 issues
// and is one of the three families #200 reintroduced, so it is precisely a family that
// has proven it regrows.
//
// DO NOT infer the boundary from the seven sites — READ IT. `eslint-plugin-sonarjs@4.2.0`
// ships 279 rules and `prefer-tag-over-role` is not among them; its README only MAPS S6819
// to the jsx-a11y rule, so the real logic lives in the analyzer and is easy to mistake for
// unknowable. It is not. Unzip the plugin jar, untar the bundle it carries, and grep the
// rule name:
//
//   /mnt/data/sonarqube/scanner-cache/*/sonar-javascript-plugin.jar
//     -> sonarjs-1.0.0.tgz -> package/bin/server.cjs   (grep "prefer-tag-over-role")
//
// Minified but legible. The decorator is `_0o`, and it reports only when a DOM pre-filter
// passes AND none of nine exemptions match:
//
//   function _0o(e){ ... Qdr({type:"JSXElement",openingElement:n}) && (L8g(n) || t.report(r)) }
//   function Qdr(e){ return e.openingElement.name.type==="JSXIdentifier" && nlm.has(...name) }
//   function L8g(e){ ...role via $Er (getLiteralPropValue), lowercased...
//                    return M8g||Q8g||U8g||Y8g||G8g||V8g||H8g||K8g||X8g }
//   function U8g(e,t){ return e==="status" && !!Xre(t,"aria-live") }
//
// Be precise about what is mirrored, because a looser summary of this got written twice
// and was wrong both times. `L8g` has NINE arms and exactly ONE of them — `U8g` — is
// reimplemented here. The other two pieces below are the wrapper (`Qdr`) and `L8g`'s
// preamble (`$Er`), not arms:
//
//   1. `Qdr`: `nlm` is a 149-entry set of native HTML tag names, so Sonar NEVER raises this
//      on a custom component. In a shadcn codebase full of wrappers, `<Alert role="status">`
//      is ordinary code and the raw rule would red the build on it.
//   2. `$Er`: role is read with `getLiteralPropValue` and lowercased. Note the base rule
//      looks roles up in a lowercase-keyed table and so returns early on `role="STATUS"`
//      anyway — the lowercasing here is defensive, not load-bearing.
//   3. `U8g`: `aria-live` is tested for PRESENCE, not value. An earlier version allowlisted
//      `polite`/`assertive` on the reasoning that `aria-live="off"` is not a live region.
//      True as accessibility advice, and wrong here — it made the gate stricter than the
//      thing it mirrors, which is the one property this whole file is built to avoid. Take
//      the a11y argument to Sonar, not to this file.
//
// The other EIGHT arms are not implemented, and all eight are named so the list cannot
// quietly shrink again: `M8g` (`<svg role="presentation"|"img" aria-hidden>`), `Q8g`
// (`<svg role="img">` with aria-label/labelledby or a `<title>` child), `Y8g` (`slider`
// with aria-valuemin/max/now), `G8g` (`radio` with aria-checked), `V8g` (`combobox` with
// aria-expanded plus one of aria-controls/owns/haspopup), `H8g` (`separator` with
// children), `K8g` (`img` role on div/span with children or a style), `X8g` (table/grid/
// listbox nesting with row/cell/option roles). None occurs in the tree today. Copying eight
// more minified predicates would turn a small mirror into an untestable fork of an
// analyzer; if this rule ever fires on one of those shapes it is a KNOWN over-fire, and the
// fix is to add that arm here, not a disable comment at the call site.
function exemptSonarAcceptedRoles(rule) {
  // Sonar's `nlm` is `aria-query`'s `dom` plus 20 names it does not carry — obsolete tags
  // and a few modern ones. Derived by diffing the two sets, not guessed; re-derive by
  // extracting `nlm=new Set([...])` from `server.cjs` and comparing with `[...dom.keys()]`.
  // Using `dom` alone would turn the over-fires this fixes into MISSES on `<svg role=...>`
  // and `<search role=...>`, which is the same bug pointing the other way.
  const SONAR_EXTRA_TAGS = new Set([
    "basefont", "bgsound", "command", "element", "image", "isindex", "listing", "math",
    "multicol", "nextid", "nobr", "noframes", "plaintext", "rb", "rbc", "search", "shadow",
    "slot", "svg", "template",
  ]);
  // Mirrors `Qdr`. This previously used `/^[a-z]/`, which is not the same test and errs in
  // the OVER-FIRE direction: `nlm` has no SVG child elements, so `<g role="navigation">`,
  // `<path>`, `<circle>`, `<text>` and every web component (`<my-el>`) are silent at Sonar
  // and were red here. `Logo.tsx` already contains `<g>`, `<path>` and `<rect>`.
  const isDomElement = (node) =>
    node.name?.type === "JSXIdentifier" &&
    (dom.has(node.name.name) || SONAR_EXTRA_TAGS.has(node.name.name));
  return {
    ...rule,
    create(context) {
      // `Object.create` rather than a spread: an ESLint rule context is frozen and
      // exposes much of its surface (`sourceCode`, `settings`, `options`) through its
      // prototype, which a spread would silently drop — `getElementType(context)` inside
      // this rule reads `context.settings`.
      const filtered = Object.create(context, {
        report: {
          value(descriptor) {
            const node = descriptor.node;
            if (node?.type === "JSXOpeningElement") {
              if (!isDomElement(node)) return;
              // `getProp`/`getLiteralPropValue` are the very helpers Sonar calls (`Xre`
              // and `$Er`). Hand-rolled equivalents were here first and diverged three
              // ways, each an over-fire: a template-literal role, a role arriving through
              // an object spread, and `ROLE=` (getProp is case-insensitive by default).
              const role = getLiteralPropValue(getProp(node.attributes, "role"));
              const lowered = typeof role === "string" ? role.toLowerCase() : undefined;
              if (lowered === "status" && getProp(node.attributes, "aria-live")) return;
            }
            context.report(descriptor);
          },
        },
      });
      return rule.create(filtered);
    },
  };
}

const a11yDecorated = {
  rules: {
    ...a11y.rules,
    "prefer-tag-over-role": exemptSonarAcceptedRoles(a11y.rules["prefer-tag-over-role"]),
  },
};

export default defineConfig(
  // Generated, vendored, or build output — never ours to lint. `schema.d.ts` in
  // particular is regenerated from the OpenAPI schema and must not be hand-edited.
  //
  // `eslint.config.js` used to be listed here, under that same "generated or vendored"
  // heading, which it plainly is not. Removed rather than reworded: no block below has a
  // `files` pattern matching `.js`, so it is unlinted either way and the entry only
  // implied a decision nobody made. Linting it is not currently possible — every block
  // needs the typed parser, and the project service rejects the file because no tsconfig
  // includes it (verified: "was not found by the project service"). That gap is not
  // theoretical: the server scan found an S1874 in this very file, on the
  // `tseslint.config` call the header above now explains.
  globalIgnores(["dist/**", "coverage/**", "node_modules/**", "src/api/schema.d.ts"]),

  {
    // An `eslint-disable` comment silences this gate; SonarQube does not honour one — its
    // suppression channel is `NOSONAR` or an issue resolution on the server. So a disable
    // comment produces exactly the #200 failure: CI green, family still reported by the
    // scan. `grep -rn eslint-disable src` returns nothing today, so this costs nothing and
    // keeps the "add another arm, not a disable comment" instruction above enforceable
    // rather than advisory.
    linterOptions: { noInlineConfig: true },
  },

  {
    // PARSING ONLY, for every source file — kept separate from rule selection
    // below because both rule blocks need it. Scoping the parser to the MAIN
    // block instead leaves test files on the default espree parser, which fails
    // on the first piece of TSX with `Parsing error: Unexpected token <`.
    //
    // CLOSED BY DEFAULT — deliberately no `js.configs.recommended` and no
    // `tseslint.configs.recommendedTypeChecked`. `base` supplies the parser and
    // the plugin objects and enables ZERO rules, so the only rules that run are
    // the ones listed in the two blocks below.
    //
    // This was not the first attempt, and the correction is worth keeping. The
    // presets were extended first, with the unwanted rules disabled one by one;
    // that produced 182 errors of which 30 came from rules nobody had selected
    // (`no-unnecessary-type-assertion`, `no-base-to-string`,
    // `no-redundant-type-constituents`, `preserve-caught-error`) — the
    // turn-it-off list is unbounded and silently regrows with every preset
    // update. An allowlist cannot drift that way, and it is the only shape that
    // honestly matches the selection rule stated above.
    files: ["**/*.{ts,tsx}"],
    extends: [tseslint.configs.base],
    languageOptions: {
      parserOptions: {
        projectService: true,
        tsconfigRootDir: import.meta.dirname,
      },
    },
  },

  {
    ...SONAR_MAIN,
    plugins: {
      sonarjs,
      "jsx-a11y": a11yDecorated,
    },
    rules: {
      // --- typescript-eslint twins of cleared families ----------------------
      "@typescript-eslint/prefer-optional-chain": "error", // S6582 — 6 issues

      // --- sonarjs twins, highest-volume first ------------------------------
      "sonarjs/deprecation": "error", // S1874 — 411 issues, REGREW in #200
      "sonarjs/prefer-read-only-props": "error", // S6759 — 151 issues
      "sonarjs/no-nested-conditional": "error", // S3358 — 74 issues
      "sonarjs/cognitive-complexity": "error", // S3776 — 16 issues
      "sonarjs/no-nested-template-literals": "error", // S4624 — 3 issues
      "sonarjs/no-clear-text-protocols": "error", // S5332 — 2 issues
      "sonarjs/prefer-regexp-exec": "error", // S6594 — 1 issue
      "sonarjs/reduce-initial-value": "error", // S6959 — 1 issue
      "sonarjs/no-identical-functions": "error", // S4144 — 1 issue

      // --- accessibility twins ----------------------------------------------
      // `eslint-plugin-jsx-a11y@6.10.2` declares peer `eslint` only up to ^9 and
      // is installed through a peer override. That is a VERSION RANGE the author
      // has not widened, not an observed incompatibility: every rule below is
      // mutation-tested in `eslint.config.test.ts` under ESLint 10, so the gate
      // proves the pairing works rather than assuming it. If a future ESLint
      // genuinely breaks the plugin, those tests are what will say so.
      "jsx-a11y/prefer-tag-over-role": "error", // S6819 — 45 issues, REGREW in #200
      // S6848 is `no-static-element-interactions`, NOT
      // `no-noninteractive-element-interactions` — that one is S6847, which this
      // project has never violated (`api/issues/search` reports 0 ever, against 3
      // for S6848, all in `ImportPlaylistsPage.tsx`). The wrong twin was pinned
      // here first, from memory; it fired 3 times on code Sonar has no complaint
      // about, which is what exposed the mistake. Both names are plausible and
      // the mapping is in the sonarjs README (lines 505-506) — read it, do not
      // recall it.
      "jsx-a11y/no-static-element-interactions": "error", // S6848 — 3 issues
      // S1082 — 3 issues. TWO rules, not one: Sonar registers S1082 as `mouse-events-a11y`
      // with an `externalRules` array of length 2, and its `create` runs both and merges
      // the visitors. Pinning only the mouse half left `<li onClick>`, `<ul onClick>` and
      // `<p onClick>` free to regrow with the gate green — measured, exit 0 — and S6848
      // does not cover them, because that rule exempts anything carrying a role, implicit
      // or explicit, which is exactly what list and text elements have. Adding the click
      // half costs 0 findings today. GENERAL RULE: any Sonar key whose `externalRules`
      // array has more than one entry needs every member enabled, or the family is pinned
      // at part of its width.
      "jsx-a11y/mouse-events-have-key-events": "error",
      "jsx-a11y/click-events-have-key-events": "error",
      "jsx-a11y/img-redundant-alt": "error", // S6851 — 2 issues
      "jsx-a11y/no-noninteractive-tabindex": "error", // S6845 — 1 issue
    },
  },

  {
    // The three TEST-scope rules in the set. Which three is not guessable from the
    // rule names — `prefer-specific-assertions` and `parameterized-tests` read as
    // test-only and are, but `no-clear-text-protocols` fires only on test fixtures
    // here and is still MAIN — so the split follows `api/rules/show`'s `scope` field
    // for every rule, not intuition. Both of these sat in the MAIN block first, where
    // they are scoped out of every file Sonar actually raises them in: a rule pinned
    // where its family cannot appear is an inert rule wearing a guard's name.
    ...SONAR_TEST,
    plugins: { sonarjs, "testing-library": testingLibrary },
    rules: {
      "testing-library/prefer-find-by": "error", // S9020 — 23 issues, REGREW in #200
      "sonarjs/prefer-specific-assertions": "error", // S5906 — 3 issues
      "sonarjs/parameterized-tests": "error", // S5976 — 1 issue
    },
  },
);
