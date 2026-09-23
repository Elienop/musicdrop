// frontend/src/pages/settings/configFailureText.ts
/** The words after "Save failed." and "Apply failed." on both config pages
 * (Settings → Beets and the Naming panel), kept in one place so they match. */

/** Apply's sentence when the server sent no recovery line. */
export const APPLY_FALLBACK =
  "Your config is saved on disk — try again or restart MusicDrop.";

/** After "Save failed.": the server's `config_on_disk` sentence plus what to
 * do next, or the fixed sentence for any other failure. */
export function saveFailureDetail(onDisk: string | null | undefined): string {
  return onDisk
    ? `${onDisk} Fix that and Save again.`
    : "Your changes weren’t written — try again.";
}
