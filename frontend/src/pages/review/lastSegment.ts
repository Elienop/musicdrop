/** Last path segment of a folder, for a row with no parsed album title and the
 * importing-now line. */
export function lastSegment(folder: string): string {
  const parts = folder.split("/").filter(Boolean);
  return parts.at(-1) ?? folder;
}
