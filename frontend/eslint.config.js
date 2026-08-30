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
// here maps to a Sonar rule this project has actually violated and fixed — 18 of the 27
// TypeScript families (768 resolved issues) that the programme cleared — and `main`
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

import tseslint from "typescript-eslint";
import sonarjs from "eslint-plugin-sonarjs";
import a11y from "eslint-plugin-jsx-a11y";
import testingLibrary from "eslint-plugin-testing-library";

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
// Scopes were read per rule from `api/rules/show`, not guessed: of the 18 enabled below,
// 15 are MAIN and THREE are TEST — S9020, S5906 and S5976. That is not deducible from the
// names, which is the whole reason the split is per-rule; `no-clear-text-protocols` fires
// only on test fixtures here and is still MAIN.
// `vite.config.ts` and `vitest.config.ts` are named because `sonar.sources=.` scans them
// as SOURCE — they match no exclusion and no test inclusion. Leaving them out let a
// MAIN-scope finding reach the server without ever failing this gate. Both are clean
// under every rule below today, so closing the gap costs nothing.
const SONAR_MAIN = {
  files: ["src/**/*.{ts,tsx}", "vite.config.ts", "vitest.config.ts"],
  ignores: ["src/**/*.test.{ts,tsx}", "src/components/ui/**"],
};
// Exactly `sonar.test.inclusions`, no wider. `src/test/**` (the vitest harness) is
// deliberately absent: Sonar treats it as SOURCE, so widening here would exempt from the
// MAIN rules a directory Sonar still scans. `eslint.config.test.ts` sits at the frontend
// root rather than under `src/`, but `sonar.test.inclusions` matches it as `**/*.test.ts`
// and Sonar scans it, so it is named here too — otherwise a TEST-scope finding in that
// file would reach the server without ever failing this gate.
const SONAR_TEST = { files: ["src/**/*.test.{ts,tsx}", "eslint.config.test.ts"] };

// --- One decorated rule ------------------------------------------------------
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
// Three consequences, each mirrored below:
//   1. `nlm` is the set of native HTML tag names, so Sonar NEVER raises this on a custom
//      component. In a shadcn codebase full of wrappers, `<Alert role="status">` is an
//      ordinary thing to write and the raw rule would red the build on it.
//   2. Role is read with `getLiteralPropValue` and lowercased, so `role={"status"}` and
//      `role="STATUS"` are the same as `role="status"`.
//   3. `U8g` tests `aria-live` for PRESENCE, not value. An earlier version of this
//      function allowlisted `polite`/`assertive` on the reasoning that `aria-live="off"`
//      is not a live region. True as accessibility advice, and wrong here — it made the
//      gate stricter than the thing it mirrors, which is the one property this whole file
//      is built to avoid. Match `U8g`; take the a11y argument to Sonar, not to this file.
//
// The other eight exemptions are NOT implemented: `slider` with aria-valuemin/max/now,
// `radio` with aria-checked, `combobox` with aria-expanded plus one of
// aria-controls/owns/haspopup, `separator` with children, `img` on div/span with children
// or a style, and two more. None occurs in the tree today. Copying nine minified
// predicates would turn a small mirror into an untestable fork of an analyzer, so they are
// named here instead: if this rule ever fires on one of those shapes, it is a known
// over-fire and the fix is another arm, not a disable comment.
function exemptSonarAcceptedRoles(rule) {
  const attrNamed = (node, name) =>
    node.attributes.find(
      (a) => a.type === "JSXAttribute" && a.name?.type === "JSXIdentifier" && a.name.name === name,
    );
  // Mirrors `$Er` (jsx-ast-utils `getLiteralPropValue`) for the shapes that reach it here:
  // a bare string, or a string wrapped in an expression container.
  const literalValue = (attr) => {
    const v = attr?.value;
    if (v?.type === "Literal") return v.value;
    if (v?.type === "JSXExpressionContainer" && v.expression.type === "Literal") {
      return v.expression.value;
    }
    return undefined;
  };
  // Mirrors `Qdr`. Sonar tests membership in an explicit native-tag set; JSX's own
  // convention — lowercase initial means DOM element, capitalised means component — gives
  // the same answer for every real tag, and differs only on a lowercase name that is not
  // valid HTML (`<foo role="...">`), where this stays silent and Sonar would report.
  const isDomElement = (node) =>
    node.name?.type === "JSXIdentifier" && /^[a-z]/.test(node.name.name);
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
              const role = literalValue(attrNamed(node, "role"));
              const lowered = typeof role === "string" ? role.toLowerCase() : undefined;
              if (lowered === "status" && attrNamed(node, "aria-live")) return;
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

export default tseslint.config(
  {
    // Generated, vendored, or build output — never ours to lint. `schema.d.ts` in
    // particular is regenerated from the OpenAPI schema and must not be hand-edited.
    //
    // `eslint.config.js` used to be listed here, under that same "generated or vendored"
    // heading, which it plainly is not. Removed rather than reworded: no block below has
    // a `files` pattern matching `.js`, so it is unlinted either way and the entry only
    // implied a decision nobody made. Linting it is not currently possible — every block
    // needs the typed parser, and the project service rejects the file because no tsconfig
    // includes it (verified: "was not found by the project service"). That is a real if
    // small gap, since `sonar.sources=.` means Sonar DOES scan it as JS source.
    ignores: ["dist/**", "coverage/**", "node_modules/**", "src/api/schema.d.ts"],
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
    files: ["src/**/*.{ts,tsx}", "eslint.config.test.ts", "vite.config.ts", "vitest.config.ts"],
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
      "jsx-a11y/mouse-events-have-key-events": "error", // S1082 — 3 issues
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
