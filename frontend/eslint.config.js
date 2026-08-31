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
// here maps to a Sonar rule this project has actually violated and fixed — 26 rules
// covering 25 of the 27 JS/TS families (769 resolved issues) that the programme cleared,
// the counts differing because S1082 is a union of two ESLint rules — and `main`
// currently sits at 0 open issues, so each one is a regression guard rather than a new
// opinion. Turning on a preset would flag code nobody has agreed to change and would make
// the gate impossible to land.
//
// Re-derive those numbers rather than quoting them; they move whenever a family is
// cleared or regrows. One command, and it needs the server's token:
//
//   T=$(cat /mnt/data/sonarqube/token); curl -s -u "$T:" \
//     'http://127.0.0.1:9000/api/issues/search?componentKeys=musicdrop&languages=ts,js,css&resolved=true&ps=1&facets=rules&facetMode=count'
//
// Read the `rules` facet, not the bare total. It returns 29 rows for 28 families: S1874
// appears twice, once under `typescript:` and once under `javascript:`, and `css:S8776`
// (4 issues) is the one CSS family, which has no JS twin and never will. An earlier count
// here said 768 because it summed only the `typescript:` rows and dropped
// `javascript:S1874`'s single issue.
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
// decorator note below cites. `decorated` means the ESLint rule is wrapped, merged, or
// both, and "wrapped" spans everything from a cosmetic message rewrite to a type-directed
// suppression that changes which lines report. So the label alone settles nothing: read
// the decorator body. Nine of the families pinned here are `decorated`:
//
//   S6819  prefer-tag-over-role         -> wrapped below, faithfully (one of nine arms)
//   S1082  mouse-events-a11y            -> a UNION of two rules; both enabled
//   S6478  no-unstable-nested-components-> wrapped below + Sonar's own looser options
//   S1186  no-empty-function            -> wrapped below + Sonar's own looser options
//   S7780  prefer-string-raw            -> raw is FAITHFUL; decorator only strips the fix
//   S6481  jsx-no-constructed-context-values -> raw is FAITHFUL; only message text changes
//   S9020  prefer-find-by               -> run RAW, minus the three settings mirrored below
//   S6582  prefer-optional-chain        -> run RAW; Sonar adds six suppression arms
//   S7755  prefer-at                    -> NOT ENABLED, see the "deliberately off" note
//
// The two that stay knowingly unmirrored are S6582 and S9020, and the earlier summary of
// both was wrong, so here is what the bundle actually says.
//
// S6582 (`B$n` in `server.cjs`) suppresses on `b(S) = u || f || d || m || A || E`. FOUR of
// the six arms — `u`, `f`, `d`, `m` — take the CONTEXTUAL type of the outermost logical
// chain and stay silent when it excludes `undefined | any | unknown | void` (`P$n`); they
// differ only in where the chain sits (`return`, a `VariableDeclarator` init, a `Property`
// value, a call argument). Arm `A` is different: the chain is the right-hand side of a
// plain `=` assignment and the type read is `getTypeAtLocation` of the ASSIGNMENT TARGET,
// not a contextual type. Arm `E` uses NO type context at all — it fires when the right
// conjunct is a `BinaryExpression` whose operator is one of `!== != < > <= >=` (note: `===`
// and `==` are NOT in that set), both sides are member expressions on differently-spelled
// objects, and both object types include `null | undefined`. Also: if the program has
// neither `strict` nor `strictNullChecks`, Sonar runs the rule RAW.
//
// Why that accuracy matters, measured. On Dependabot PR #204 — which changes ONLY
// `package.json` and `package-lock.json`, no source — this raw rule reports 3 errors it
// does not report on `main`: `ImportPlaylistsPage.tsx:399`, `PlaylistsPage.tsx:58` and
// `ArtistArtPanel.tsx:117`, all the shape `X && X.prop === literal` inside a JSX
// expression container. A type-aware rule's output moves when the types move, and #204
// bumps 23 packages including `@tanstack/react-query` and `@types/react`, which is where
// those three receivers get their types. The instinct is to call that an over-fire and
// suppress it. Read the arms first: none of the six matches a JSX expression container
// (`u`/`f`/`d`/`A` need a return, declarator, property or assignment parent, and `m` needs
// a call argument), and arm `E`'s operator set excludes `===`. So Sonar would report these
// too — this is the gate working, not over-firing, and the fix belongs in the source.
//
// S9020's old note here claimed Sonar "suppresses on type". That is FALSE: its own
// registration sets `requiresTypeChecking: false`. What the decorator (`UGo`) actually
// does is inject three `eslint-plugin-testing-library` settings, all `"off"` — those are
// mirrored on the TEST block below — plus one arm this file does NOT mirror (`jAh`): it
// suppresses when the `waitFor(() => X.getBy…())` receiver's fully-qualified name resolves
// to something outside `@testing-library.`. An unresolvable name is NOT suppressed. That
// arm needs Sonar's own module-name resolver; the three settings already cover most of the
// same ground and are pure narrowing, so the residue is recorded rather than ported.
//
// If either rule ever fires on something Sonar is silent about, this comment is the place
// to start, not a disable comment.
//
// --- DELIBERATELY OFF, with the reason, so nobody "finishes the job" by accident --------
//
// Two cleared families have a twin that this gate must NOT enable, because the raw rule
// reports code SonarQube accepts. A gate stricter than the thing it mirrors fails CI on
// code the server passes, which turns every dependency bump into a disable comment.
//
//   S7755  unicorn/prefer-at  (2 issues, all FIXED)
//     Decorator `Mph` reports ONLY when `Dc(parserServices)` (full type information) AND
//     `ulr(receiver, "at", services)` — the receiver's type has an `at` member that is a
//     method, or a property with call signatures. With no type info it reports NOTHING.
//     Mirroring that means walking the receiver back through the member/call chain and
//     asking the TypeScript checker for a callable `.at()`, i.e. importing the type
//     checker into this file. The raw rule finds 0 sites today, so enabling it would be a
//     silent bet that the first future hit happens to be one Sonar also reports.
//
//   S6479  react/no-array-index-key  (3 issues, all FIXED)
//     MEASURED, not predicted: the raw rule reports 9 sites on `main` — 5 in
//     `ReorganizeControl.tsx`, 4 in `DiskSyncPanel.tsx` — and Sonar reports 0 open S6479
//     against that same code (last `main` analysis 2026-08-30T16:54Z, all 3 historical
//     issues resolved FIXED, none FALSE-POSITIVE or WONTFIX). Every one of the 9 is
//     `key={`${x.label}-${i}`}`, which Sonar drops on its `LBg` arm: a `TemplateLiteral`
//     with more than one expression. `LBg` is two lines and porting it would silence all
//     nine — but the other arm, `MBg`, is not portable at that price: it walks up to the
//     enclosing `.map`/`filter`/`reduce` callback, matches the index parameter by name,
//     and then decides whether the receiver is a compile-time-constant array via
//     `g$n` (nested array literals), `GBg` (`Array.from({length: <literal>})`) and `Qho`
//     (const-variable resolution with write/escape analysis over the scope graph). Leaving
//     `MBg` out keeps a live over-fire on the ordinary `[1, 2, 3].map((n, i) => <Skeleton
//     key={i} />)` skeleton idiom. Copying scope-and-mutation analysis out of a minified
//     analyzer is the "untestable fork" this file already refuses to build once, so the
//     rule stays off and this paragraph is the record.

// `defineConfig`/`globalIgnores` come from ESLint core, not from `tseslint.config`.
// typescript-eslint deprecated its own helper once core shipped the same functionality
// (`config-helper.d.ts:67`), and the deprecated signature is a `javascript:S1874` — the
// very family #200 regrew. That one was caught by the server scan, because at the time
// this file escaped the gate: no block matched a `.js` file and no tsconfig included it,
// so the type-aware rules could not run on it. That hole is now closed: the block below
// scoped to `eslint.config.js` pins the deprecation family on this file itself, and
// `tsconfig.node.json` includes it so the typed parser can resolve what the rule points at.
import { defineConfig, globalIgnores } from "eslint/config";
import tseslint from "typescript-eslint";
import sonarjs from "eslint-plugin-sonarjs";
import a11y from "eslint-plugin-jsx-a11y";
import testingLibrary from "eslint-plugin-testing-library";
// PINNED TO WHAT THE ANALYZER RUNS, not to what npm calls latest. `package/package.json`
// inside the same bundle the decorator notes cite lists the exact plugin versions SonarJS
// executes: `eslint-plugin-unicorn` 65.0.1, `eslint-plugin-react` 7.37.5,
// `eslint-plugin-jsx-a11y` 6.10.2, `eslint-plugin-testing-library` 7.16.2. Tracking a
// newer major would silently make this gate a DIFFERENT linter from the one it mirrors —
// unicorn 74, the current latest, is nine majors ahead and has already renamed rules the
// sonarjs README still maps by their old ids (`no-array-for-each` -> `no-for-each`, S7728),
// which is a hard config error rather than a silent no-op. So the caret ranges in
// `package.json` are `^65.0.1` and `^7.37.5` on purpose; a Dependabot major on either is a
// prompt to re-read the bundle, not a routine bump.
//
// A MINOR can move a mirrored rule too, so the pin is asserted rather than trusted:
// `eslint.config.test.ts` compares the four installed plugin versions against the ones the
// bundle declares and fails on any drift. The precedent is `propNamePattern`, the option
// the S6478 mirror leans on — it arrived in a 7.3x minor, and the plugin's own default is
// narrower. A caret admits exactly that kind of change silently.
//
// `typescript-eslint` is the fifth plugin behind a mirrored rule and is deliberately NOT
// pinned this way: the repo floats `^8.68.0` while the bundle declares 8.65.0, backing both
// `sonar-mirror/no-empty-function` and the raw `@typescript-eslint/prefer-optional-chain`.
// Checked at the time of writing — `dist/rules/no-empty-function.js` and
// `dist/rules/prefer-optional-chain.js` are byte-identical between the two versions apart
// from a trailing sourceMappingURL — so the divergence is real but currently inert. It is
// named here because an unnamed divergence is the one nobody re-checks.
//
// One asymmetry the gate cannot express: S7780, S7776 and S7760 all register
// `skipOnGeneratedSource: true`, and the analyzer skips them on any file the scanner tags
// as generated. ESLint has no equivalent notion, so on such a file the gate would report
// where Sonar stays silent. Theoretical today — nothing under `frontend/` is tagged, and
// `src/api/schema.d.ts`, the one generated artifact, sits in both `globalIgnores` and
// `sonar.exclusions` — but it is the same class of divergence documented for S6582 below,
// and it would bite the moment a generated file lands outside those ignores.
import react from "eslint-plugin-react";
import unicorn from "eslint-plugin-unicorn";
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

// --- S1186's decorator: only three SHAPES of empty function are reported ------
//
// `@typescript-eslint/no-empty-function` is S1186's twin, and the options alone do not get
// there. Sonar passes `allow: ["arrowFunctions", "constructors", "private-constructors"]`
// — mirrored on the rule entry below — and then wraps the result in `Wia`/`yMf`, which
// throws away every report whose node is not one of exactly three shapes (`_Mf`):
//
//   1. a `FunctionDeclaration`,
//   2. the value of a class `MethodDefinition`,
//   3. the init of a `VariableDeclarator`,
//
// and, in each case, only when the corresponding NAME is not an event handler or a noop
// (`lCn`: an `Identifier` matching `/^on[A-Z]/` or `/noop/i`). Everything else is silent
// at Sonar: an empty callback argument (`vi.fn(() => {})`, `foo(function () {})`), an
// object-literal method (`{ m() {} }` — its parent is a `Property`, not a
// `MethodDefinition`), an empty function in a JSX prop, an empty IIFE. The raw rule
// reports all of them, so running it raw would fail the build on ordinary test doubles.
//
// `lCn` reads the name through Sonar's `Ei`, which accepts an `Identifier` and nothing
// else, so the exemption is narrower than it looks. Measured against this config:
// `class K { onFoo() {} }` is exempt but `class K { ["onFoo"]() {} }` REPORTS, because a
// computed key is not an `Identifier`. Mirrored rather than "improved" in either
// direction: a stricter reading fails CI on code the server passes, and a looser one hands
// back the family. (The other two halves, also measured: `onclick` reports — `^on[A-Z]` is
// case-sensitive — and `makeNoopHandler` is exempt, since `/noop/i` is unanchored.)
function reportOnlyNamedEmptyFunctionShapes(rule) {
  // Mirrors `lCn`. Note both halves: `^on[A-Z]` is anchored and case-SENSITIVE (so
  // `onclick` is still reported), `noop` is unanchored and case-INSENSITIVE (so
  // `makeNoopHandler` is exempt).
  const isHandlerOrNoopName = (id) =>
    id != null && id.type === "Identifier" && (/^on[A-Z]/.test(id.name) || /noop/i.test(id.name));
  // Mirrors `_Mf`, kept as the same three-way disjunction rather than an early return per
  // shape, so a future reader can diff it against the minified original line for line.
  const isSonarReportedShape = (node) => {
    const parent = node.parent;
    return (
      (node.type === "FunctionDeclaration" && !isHandlerOrNoopName(node.id)) ||
      (parent?.type === "MethodDefinition" &&
        parent.value === node &&
        !isHandlerOrNoopName(parent.key)) ||
      (parent?.type === "VariableDeclarator" &&
        parent.init === node &&
        !isHandlerOrNoopName(parent.id))
    );
  };
  return {
    ...rule,
    create(context) {
      const filtered = Object.create(context, {
        report: {
          value(descriptor) {
            const node = descriptor.node;
            // Mirrors `AMf`: no node, or a node the traversal never linked into the tree,
            // and Sonar drops the report entirely rather than passing it through.
            if (!node || typeof node.type !== "string" || !("parent" in node)) return;
            if (!isSonarReportedShape(node)) return;
            context.report(descriptor);
          },
        },
      });
      return rule.create(filtered);
    },
  };
}

// --- S6478's decorator: react-intl render props are not nested components -----
//
// Two separate loosenings, and the options are only the first. Sonar's registration
// carries `fields` defaults of `allowAsProps: false` and `propNamePattern:
// "{render*,*Enhancer,*Renderer}"`; the plugin's own default is the narrower `"render*"`,
// so passing Sonar's value is a WIDENING of what counts as a render prop and therefore a
// narrowing of what reports. Both are set explicitly on the rule entry below — including
// `allowAsProps: false`, which equals the plugin default, because writing the pair keeps
// the config diffable against the bundle's `fields` array.
//
// The second is `RBg`, mirrored here: an inline function that is a `Property` of an
// `ObjectExpression` is exempt when that object is either the `values` prop of a react-intl
// element (`OBg` — `<FormattedMessage values={{ b: (c) => <b>{c}</b> }} />`) or the second
// argument of a `…formatMessage(…)` call (`FBg`). MusicDrop does not use react-intl today;
// the arm is mirrored anyway because the alternative is a decorator that is *nearly* the
// analyzer, which is how the a11y mirror above went wrong three times.
function exemptReactIntlRenderProps(rule) {
  // `$t` is in Sonar's set alongside the three `Formatted*` components; it is the
  // Vue-i18n-style alias, kept so this set is a transcription rather than a summary.
  const REACT_INTL_ELEMENTS = new Set([
    "FormattedMessage",
    "FormattedHTMLMessage",
    "FormattedPlural",
    "$t",
  ]);
  // Mirrors `OBg`.
  const isIntlValuesProp = (container) => {
    const attribute = container.parent;
    if (attribute?.type !== "JSXAttribute") return false;
    const element = attribute.parent;
    if (element?.type !== "JSXOpeningElement") return false;
    return (
      attribute.name.type === "JSXIdentifier" &&
      attribute.name.name === "values" &&
      element.name.type === "JSXIdentifier" &&
      REACT_INTL_ELEMENTS.has(element.name.name)
    );
  };
  // Mirrors `FBg`. The object must be argument index 1 exactly, and the callee a member
  // expression whose property is `formatMessage` — the receiver is not checked.
  const isFormatMessageValuesArgument = (call, object) => {
    const callee = call.callee;
    const property = callee.type === "MemberExpression" ? callee.property : null;
    return (
      call.arguments[1] === object && property?.type === "Identifier" && property.name === "formatMessage"
    );
  };
  // Mirrors `RBg`.
  const isReactIntlRenderProp = (node) => {
    if (node.type !== "ArrowFunctionExpression" && node.type !== "FunctionExpression") return false;
    const property = node.parent;
    const object = property?.parent;
    const outer = object?.parent;
    if (property?.type !== "Property" || object?.type !== "ObjectExpression") return false;
    if (outer?.type === "JSXExpressionContainer") return isIntlValuesProp(outer);
    if (outer?.type === "CallExpression") return isFormatMessageValuesArgument(outer, object);
    return false;
  };
  return {
    ...rule,
    create(context) {
      const filtered = Object.create(context, {
        report: {
          value(descriptor) {
            const node = descriptor.node;
            if (node && isReactIntlRenderProp(node)) return;
            context.report(descriptor);
          },
        },
      });
      return rule.create(filtered);
    },
  };
}

const reactDecorated = {
  rules: {
    ...react.rules,
    "no-unstable-nested-components": exemptReactIntlRenderProps(
      react.rules["no-unstable-nested-components"],
    ),
  },
};

// A NAMESPACE OF OUR OWN, and not a stylistic choice. `jsx-a11y` above can be re-registered
// under its own name because nothing else registers it; `@typescript-eslint` cannot, because
// `tseslint.configs.base` in the parsing block already claims that key, and ESLint 10 rejects
// a second definition outright — measured: `Config (unnamed): Key "plugins": Cannot redefine
// plugin "@typescript-eslint"`. Shadowing the base config instead (hand-rolling parser +
// plugin here) would drift from whatever `base` becomes on the next typescript-eslint bump,
// so the decorated rule gets its own key. Grepping for
// `@typescript-eslint/no-empty-function` therefore finds this comment and not a rule entry:
// the live rule id is `sonar-mirror/no-empty-function`, and it is the S1186 twin.
const sonarMirror = {
  rules: {
    "no-empty-function": reportOnlyNamedEmptyFunctionShapes(
      tseslint.plugin.rules["no-empty-function"],
    ),
  },
};

export default defineConfig(
  // Generated, vendored, or build output — never ours to lint. `schema.d.ts` in
  // particular is regenerated from the OpenAPI schema and must not be hand-edited.
  //
  // `eslint.config.js` used to be listed here, under that same "generated or vendored"
  // heading, which it plainly is not. Removed rather than reworded: at the time no block
  // had a `files` pattern matching `.js`, so it was unlinted either way and the entry
  // only implied a decision nobody made. That has since changed — the block below scoped
  // to `eslint.config.js` lints it for the deprecation family, and `tsconfig.node.json`
  // includes it so the typed parser can resolve the file — which matters because the
  // server scan found an S1874 in this very file, on the `tseslint.config` call the
  // header above now explains. It stays out of this ignore list because it is ours to lint.
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
    // The gate linting itself. This file used to be the one file SonarQube scans that
    // zero ESLint rules reached — every rule block needs the typed parser, and it took a
    // server-scan `javascript:S1874` (the deprecated `tseslint.config` call, header above)
    // to prove the gap was live. That cannot happen twice: the deprecation family is now
    // pinned here on this file, so the same escape reddens the build instead of the scan.
    // It is deliberately narrower than the MAIN block: only the one family #200 regrew
    // in this file, not the full allowlist, which was never agreed for a `.js` config.
    // `tsconfig.node.json` must keep `allowJs: true` and list this file, or the typed
    // parser drops it and the rule goes inert — the mutation case in
    // `eslint.config.test.ts` is what says so if that happens.
    files: ["eslint.config.js"],
    extends: [tseslint.configs.base],
    languageOptions: {
      parserOptions: {
        projectService: true,
        tsconfigRootDir: import.meta.dirname,
      },
    },
    plugins: { sonarjs },
    rules: { "sonarjs/deprecation": "error" }, // S1874 — the family that escaped via this file
  },

  {
    ...SONAR_MAIN,
    plugins: {
      sonarjs,
      "jsx-a11y": a11yDecorated,
      react: reactDecorated,
      unicorn,
      "sonar-mirror": sonarMirror,
    },
    // TRANSCRIBED FROM THE ANALYZER, not chosen. SonarJS builds every file's config with
    // `settings: { react: { version: "999.999.999" }, … }` — the plugin's own
    // `ULTIMATE_LATEST_SEMVER` sentinel, meaning "assume the newest React". It never
    // detects.
    //
    // Two things break if this is "improved". `version: "detect"` CRASHES on ESLint 10:
    // `eslint-plugin-react`'s `resolveBasedir` calls `context.getFilename()`
    // (`lib/util/version.js:31`), which ESLint 10 removed, so every rule routed through
    // `Components.detect` dies at load with `contextOrFilename.getFilename is not a
    // function` — measured here on `no-unstable-nested-components` and
    // `jsx-no-constructed-context-values`, while `no-array-index-key` and
    // `jsx-child-element-spacing` survive because they never take that path. And omitting
    // the setting entirely is not free either: the plugin then prints "React version not
    // specified" to stderr on every run and falls back to the same 999.999.999 anyway.
    //
    // Pinning the INSTALLED react version instead would be the intuitive fix and is the
    // wrong one — it makes any version-sensitive rule behave differently from the server,
    // which is the failure mode this whole file is built to avoid. The sentinel is not a
    // stale literal to be kept in sync with `react` in `package.json`; it is the value the
    // analyzer uses, and `eslint.config.test.ts` pins it as such.
    settings: { react: { version: "999.999.999" } },
    rules: {
      // --- typescript-eslint twins of cleared families ----------------------
      "@typescript-eslint/prefer-optional-chain": "error", // S6582 — 6 issues
      // S1186 — 3 issues. Both loosenings are load-bearing; see
      // `reportOnlyNamedEmptyFunctionShapes` above for why the raw rule reports test
      // doubles Sonar accepts. The `allow` list is Sonar's `fields` default verbatim.
      "sonar-mirror/no-empty-function": [
        "error",
        { allow: ["arrowFunctions", "constructors", "private-constructors"] },
      ],

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

      // --- unicorn twins ----------------------------------------------------
      // `eslint-plugin-unicorn@65.0.1` needs NO peer override: it declares
      // `eslint: ">=9.38.0"`, which ESLint 10.9.1 satisfies. Only `eslint-plugin-react`
      // needed one, and only because its range stops at `^9.7`.
      //
      // All three run RAW and that is faithful, not a shortcut. S7776 and S7760 are
      // `implementation: "external"` — no decorator exists. S7780 IS `decorated`, but its
      // decorator (`uUo`) reports on BOTH branches: it strips `fix`/`suggest` when the
      // literal sits on a template-literal line and otherwise passes the descriptor
      // through untouched. The reported SET is identical, and the gate never runs `--fix`.
      "unicorn/prefer-string-raw": "error", // S7780 — 5 issues
      "unicorn/prefer-set-has": "error", // S7776 — 2 issues
      "unicorn/prefer-default-parameters": "error", // S7760 — 1 issue

      // --- react twins -------------------------------------------------------
      // `eslint-plugin-react@7.37.5` caps its `eslint` peer at `^9.7` and is installed
      // through a scoped override, same shape as `jsx-a11y`. Unlike `jsx-a11y` this one is
      // NOT merely an un-widened range — parts of the plugin genuinely break on ESLint 10.
      // The break is narrow and fully explained by the `settings.react.version` note above:
      // with the sentinel set, all three rules below load and fire, and
      // `eslint.config.test.ts` proves it under the ESLint the repo actually runs.
      //
      // S6772 is `external` — raw is faithful. S6481 IS `decorated`, but its decorator
      // (`Uho`) only rewrites message text, stripping a ` (at line N)` suffix; the reported
      // set is untouched, so raw is faithful there too.
      "react/jsx-child-element-spacing": "error", // S6772 — 2 issues
      "react/jsx-no-constructed-context-values": "error", // S6481 — 1 issue
      // S6478 — 2 issues. Options are Sonar's `fields` defaults verbatim (the plugin's own
      // `propNamePattern` default is the narrower `"render*"`), and the rule object is
      // decorated with Sonar's react-intl arm; see `exemptReactIntlRenderProps` above.
      "react/no-unstable-nested-components": [
        "error",
        { allowAsProps: false, propNamePattern: "{render*,*Enhancer,*Renderer}" },
      ],
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
    // The mirrorable half of S9020's decorator, transcribed from `yAh` in `server.cjs`:
    // SonarJS wraps `create` and shadows `context.settings` with exactly these three
    // entries before handing the context to the rule. They switch OFF the plugin's
    // Aggressive Reporting — module, renders and queries — so the rule considers only
    // built-in queries reached through a real `@testing-library/*` import instead of
    // guessing at custom wrappers.
    //
    // This is a pure NARROWING: it can only make the gate report less, which is the safe
    // direction for a gate that must never be stricter than the server. It costs 0
    // findings today (the suite imports `screen`/`waitFor` straight from
    // `@testing-library/react`), and the existing mutation case still reddens, which is
    // what proves the rule did not go inert.
    settings: {
      "testing-library/utils-module": "off",
      "testing-library/custom-renders": "off",
      "testing-library/custom-queries": "off",
    },
    rules: {
      "testing-library/prefer-find-by": "error", // S9020 — 23 issues, REGREW in #200
      "sonarjs/prefer-specific-assertions": "error", // S5906 — 3 issues
      "sonarjs/parameterized-tests": "error", // S5976 — 1 issue
    },
  },
);
