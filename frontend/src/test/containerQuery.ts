/**
 * Read the Tailwind container-query wiring out of a rendered subtree: which
 * variants are THERE, and which of them name a container no ancestor declares.
 *
 * A container query resolves against an ANCESTOR query container, never the
 * element itself, and a name that matches nothing simply never applies — no
 * build error, no runtime error, the element just keeps the base arm. That is
 * the same silent-failure shape as an unknown utility (repo rule: verify a
 * token exists), so it is worth a check.
 *
 * This says nothing about layout: jsdom computes none. It only pins the wiring
 * between `@container/<name>` and the `@<query>/<name>:` variants that read it.
 *
 * Declaring a container is not free: `container-type: inline-size` also makes
 * the element a containing block and a stacking context, so an absolutely
 * positioned or z-indexed descendant resolves against it from then on.
 */

/** Matches `@<query>/<name>:<utility>`, capturing `<name>`. Spelled with
 * placeholders, not a real class: Tailwind v4 scans comments, so a working
 * example here would emit a live CSS rule nothing renders.
 * Unnamed variants are skipped — an unnamed query matches the nearest container
 * of ANY name, so the name mismatch checked below cannot happen to them. */
const VARIANT = /^@[^/\s:]+\/([A-Za-z0-9_-]+):/;

/** Every container-query variant class in `root`'s subtree, sorted, deduped.
 *
 * Pair it with {@link unwiredContainerQueries}: an empty unwired list also means
 * "this subtree has no variants at all", so a test that only asserts the
 * complement stays green when the whole container-query layer is deleted. */
export function containerQueryVariants(root: HTMLElement): string[] {
  const found = new Set<string>();
  for (const el of root.querySelectorAll<HTMLElement>("*")) {
    for (const cls of el.classList) {
      if (VARIANT.test(cls)) found.add(cls);
    }
  }
  return [...found].sort(byCodeUnit);
}

/** Code-unit order — the default `sort()` order, spelled out.
 *
 * A bare `sort()` is `typescript:S2871`. The rule's own suggestion for a string
 * array is `localeCompare`, which is the right default for text a person reads;
 * these are CLASS NAMES, so the ordering that keeps a failing assertion legible
 * is the one the reader can reproduce by eye — and `localeCompare` is
 * locale- and ICU-dependent, which a test oracle should not be. */
function byCodeUnit(a: string, b: string): number {
  if (a < b) return -1;
  return a > b ? 1 : 0;
}

/** Variant classes in `root`'s subtree with no matching ancestor container. */
export function unwiredContainerQueries(root: HTMLElement): string[] {
  const unwired: string[] = [];
  for (const el of root.querySelectorAll<HTMLElement>("*")) {
    for (const cls of el.classList) {
      const name = VARIANT.exec(cls)?.[1];
      if (name === undefined) continue;
      // `@` and `/` are not class-selector characters; escape both.
      if (el.parentElement?.closest(String.raw`.\@container\/${name}`) == null) {
        unwired.push(cls);
      }
    }
  }
  return unwired;
}
