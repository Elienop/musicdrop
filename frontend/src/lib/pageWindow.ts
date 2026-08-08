/**
 * The numbered page window for `Pagination`'s full form (sm+): 1, totalPages,
 * and the current page ±2, with "gap" standing in for a run of 2+ hidden
 * pages (never a single hidden page — showing it outright reads better than
 * an ellipsis for one number). Pure and side-effect free so the table tests
 * in pageWindow.test.ts double as the spec.
 */
export function pageWindow(page: number, totalPages: number): (number | "gap")[] {
  const SIBLINGS = 2;
  if (totalPages <= 2 * SIBLINGS + 5) {
    // 1 + gap + window + gap + last can't be shorter than just listing them.
    return Array.from({ length: totalPages }, (_, i) => i + 1);
  }
  const start = Math.max(2, page - SIBLINGS);
  const end = Math.min(totalPages - 1, page + SIBLINGS);
  const out: (number | "gap")[] = [1];
  if (start === 3) out.push(2);
  else if (start > 3) out.push("gap");
  for (let p = start; p <= end; p++) out.push(p);
  if (end === totalPages - 2) out.push(totalPages - 1);
  else if (end < totalPages - 2) out.push("gap");
  out.push(totalPages);
  return out;
}
