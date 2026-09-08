/**
 * Find Tailwind container-query variants whose named container is not declared
 * on any ancestor.
 *
 * A container query resolves against an ANCESTOR query container, never the
 * element itself, and a name that matches nothing simply never applies — no
 * build error, no runtime error, the element just keeps the base arm. That is
 * the same silent-failure shape as an unknown utility (repo rule: verify a
 * token exists), so it is worth a check.
 *
 * This says nothing about layout: jsdom computes none. It only pins the wiring
 * between `@container/<name>` and the `@<query>/<name>:` variants that read it.
 */

/** `@min-[18rem]/panel:flex-row` -> `panel`; also `@md/card:`, `@max-sm/x:`. */
const VARIANT = /^@[^/\s:]+\/([A-Za-z0-9_-]+):/;

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
