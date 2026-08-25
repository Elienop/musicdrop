import { Fragment, useEffect, useRef, useState } from "react";

import {
  type PlexSectionInfo,
  type PlexSettings,
  usePlexSections,
  usePlexSettings,
  useSavePlexSettings,
  useTestPlex,
} from "@/api/usePlex";
import type { components } from "@/api/schema";
import { Spinner, Success, Warning } from "@/components/icons";
import { SettingsSection } from "@/components/system/SettingsSection";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

/** A path as its segments, the way the backend effectively reads it:
 * `translate_path` joins the root onto the track's relative path and
 * normalizes, so a trailing (or doubled) slash changes nothing and must not
 * read as a mismatch. Absoluteness survives as a leading empty segment —
 * "data" is a different root from "/data" to `os.path.join`, and only the
 * second one can ever match what Plex reports. */
function pathParts(p: string) {
  const trimmed = p.trim();
  const parts = trimmed.split("/").filter(Boolean);
  return trimmed.startsWith("/") ? ["", ...parts] : parts;
}

/** The same folder, spelled differently ("/data/music/" vs "/data/music"). */
function samePath(a: string, b: string) {
  const x = pathParts(a);
  const y = pathParts(b);
  return x.length === y.length && x.every((segment, i) => segment === y[i]);
}

/** Does a configured Library path AGREE with a folder Plex reported — is it
 * that folder, or somewhere inside it? Plex indexes a library folder
 * recursively, so a library rooted at "/data" holds every track
 * `translate_path` rebases onto "/data/music": demanding equality would flag a
 * working setup as broken. Compared segment by segment, because "/data/musicians"
 * merely starts with the STRING "/data/music" and is not inside it. */
function pathInside(folder: string, path: string) {
  const root = pathParts(folder);
  const parts = pathParts(path);
  return parts.length >= root.length && root.every((segment, i) => segment === parts[i]);
}

/** Which fetched section the current selection resolves to, mirroring the
 * backend's own rule (`app/plex/client.py: music_section`): a set title matches
 * case-insensitively (approximately — JS `toLowerCase` and Python `casefold`
 * diverge on ß-class characters, so a "Straße" library saved as "STRASSE"
 * resolves on the server but not here; the panel then merely claims nothing),
 * and a blank title ("Auto") resolves ONLY when the server
 * has exactly one music library — with several the backend refuses to guess, so
 * neither may the panel. `undefined` means "nothing known", which is also what a
 * failed or unconfigured probe produces, and the panel then claims nothing. */
function resolveSection(sections: PlexSectionInfo[], title: string) {
  const wanted = title.trim().toLowerCase();
  if (!wanted) return sections.length === 1 ? sections[0] : undefined;
  return sections.find((section) => section.title.toLowerCase() === wanted);
}

/** Who put the value currently in the Library path field there. The section
 * auto-fill may only ever REPLACE a value the panel itself authored, and no
 * value can be recognised as authored by looking at it — a user is free to type
 * the very folder Plex reports — so authorship is remembered, not inferred:
 *  - "loaded": it came from the saved settings, so the panel has no record of
 *    who wrote it (its own earlier fill and a hand-typed, saved value are
 *    indistinguishable). `handleSectionChange` claims it in exactly one case.
 *  - "panel": the exact string the auto-fill wrote, this session.
 *  - "typed": the user edited the field. Theirs, whatever it happens to say. */
type PathOrigin = { kind: "loaded" } | { kind: "panel"; value: string } | { kind: "typed" };

/** Settings → Plex: the single-account connection (base URL + write-only admin
 * token + the music-library path as Plex sees it) plus a connection test. The
 * token is write-only — the API returns only `has_token`, so the field shows a
 * "saved" placeholder and is sent only when the user types a replacement. */
export function PlexSettingsPanel() {
  const settings = usePlexSettings();

  if (settings.isPending) {
    return (
      <Panel>
        <p className="text-muted-foreground text-sm" role="status">
          Loading Plex settings…
        </p>
      </Panel>
    );
  }
  if (settings.isError || !settings.data) {
    return (
      <Panel>
        <p className="text-destructive text-sm" role="alert">
          Could not load Plex settings.
        </p>
      </Panel>
    );
  }
  return (
    <Panel>
      <PlexSettingsEditor initial={settings.data} />
    </Panel>
  );
}

function PlexSettingsEditor({ initial }: { initial: PlexSettings }) {
  const save = useSavePlexSettings();
  const test = useTestPlex();
  // Probe the server's music sections for the dropdown. A 409 (Plex not
  // configured) or any failure just leaves the list empty (retry: false) — the
  // current saved value stays selectable regardless, so a failed probe never
  // hides it.
  const sections = usePlexSections();

  const [baseUrl, setBaseUrl] = useState(initial.base_url);
  const [token, setToken] = useState("");
  const [libraryPath, setLibraryPath] = useState(initial.library_path);
  const [librarySection, setLibrarySection] = useState(initial.library_section);
  // Drives the single, always-mounted polite live region below. A live region
  // must already exist when its text changes to be reliably announced, so we
  // set this state from the save/test handlers rather than conditionally
  // mounting the confirmation nodes.
  const [statusMsg, setStatusMsg] = useState("");
  // Who authored what is in the Library path field (see PathOrigin). A ref, not
  // state: nothing renders from it, and the pick handler needs the authorship
  // as of the click.
  const pathOrigin = useRef<PathOrigin>({ kind: "loaded" });

  // Reseed the inputs when the persisted snapshot changes (e.g. after a Save
  // refetch) WITHOUT remounting. A remount-on-key would destroy the focused
  // Save button and strand keyboard focus on <body> — most visibly on the first
  // token save, which flips `has_token`. Keyed on the snapshot's field identity
  // so an unchanged background refetch leaves an in-progress edit alone.
  useEffect(() => {
    setBaseUrl(initial.base_url);
    setLibraryPath(initial.library_path);
    setLibrarySection(initial.library_section);
    setToken("");
    // The field now holds the server's value, so whatever the panel wrote (or
    // the user typed) before is no longer what is on screen.
    pathOrigin.current = { kind: "loaded" };
  }, [initial.base_url, initial.library_path, initial.library_section, initial.has_token]);

  // The form differs from what's saved on the server. "Test connection" probes
  // the SAVED config (not the typed-but-unsaved values), so testing a dirty form
  // would silently test stale settings — disable Test until the user Saves.
  const dirty =
    baseUrl !== initial.base_url ||
    libraryPath !== initial.library_path ||
    librarySection !== initial.library_section ||
    token.trim().length > 0;

  // Always keep the current saved value selectable, even if the probe failed or
  // it's no longer among the server's sections.
  const fetchedSections = sections.data?.sections ?? [];
  const sectionTitles = fetchedSections.map((section) => section.title);
  const sectionOptions =
    librarySection && !sectionTitles.includes(librarySection)
      ? [librarySection, ...sectionTitles]
      : sectionTitles;

  // The folders Plex itself reports for the selected library — the truth the
  // Library path has to agree with, since every sync rebases beets paths onto
  // it. Empty when the probe failed, Plex is unconfigured, or the selection
  // can't be resolved: the panel then shows and claims nothing.
  const selectedSection = resolveSection(fetchedSections, librarySection);
  const folders = selectedSection?.locations ?? [];
  const pathIsBlank = libraryPath.trim() === "";
  const pathMismatch =
    folders.length > 0 && !pathIsBlank && !folders.some((folder) => pathInside(folder, libraryPath));

  // "Auto" on a server with several music libraries is not a choice the backend
  // will make: `music_section` raises rather than take the first, so every sync
  // fails until a library is picked. Nothing else here would say so — "Test
  // connection" never looks at a section, so it reports a cheerful OK — and the
  // folder list above stays deliberately silent for the same reason the backend
  // refuses. So say it.
  const autoIsAmbiguous = !librarySection.trim() && fetchedSections.length > 1;

  function handleSectionChange(nextTitle: string) {
    setLibrarySection(nextTitle);
    const next = resolveSection(fetchedSections, nextTitle);
    // Fill the Library path in from Plex's own answer, but only over a value
    // the PANEL put there — never over one the user did, and never authoring
    // one from nothing.
    //  - Unambiguous: the chosen library reports EXACTLY ONE folder. When it
    //    spans several, only the user knows which one their beets root maps
    //    onto, so we list them all and fill nothing.
    //  - Ours to replace: `pathOrigin` records who wrote what is in the field,
    //    because the value cannot say. A typed one stands even when it happens
    //    to equal a folder Plex reports — picking a multi-folder library and
    //    typing the one of its folders that your beets root maps onto is the
    //    supported way to configure exactly that, and overwriting it with some
    //    other library's `locations[0]` destroys a value no pick can bring
    //    back. The warning above is the whole remedy for a typed path that
    //    looks wrong.
    //  - A freshly loaded value has no recorded author, and "replace only what
    //    we wrote" cannot bootstrap without claiming one (nothing would ever be
    //    filled, since the panel would never write a first value). So it is
    //    claimed in exactly one case: it is the SOLE folder of the library
    //    selected until now — the only string an auto-fill could have produced,
    //    and one that re-picking that library always restores. Compared by
    //    folder, not "inside it": a path UNDER that folder was narrowed by hand.
    // Blank needs no guard of its own — an empty field equals no reported
    // folder, and is not something the panel ever wrote — but it is left ALONE
    // by design, not by accident, though it looks like an empty slot. Blank is a
    // setting — "Plex sees the same paths I do" — and it is the CORRECT one
    // whenever the two share a mount. A library rooted at "/data" whose shared
    // music root is "/data/music" would be filled in as "/data", and after Save
    // `translate_path` rebases every track one level too high, so every lookup
    // misses: the exact failure this panel exists to prevent. No endpoint tells
    // the panel MusicDrop's own library root, so it cannot tell that blank from
    // a merely unconfigured one; the hint under the field explains the choice
    // instead of guessing.
    // The fill only ever runs on an explicit pick, never from an effect, so a
    // background sections refetch can't rewrite the field mid-edit — and
    // nothing is persisted until Save, so the change is visible and undoable.
    if (next?.locations.length !== 1) return;
    const origin = pathOrigin.current;
    const ours =
      origin.kind === "panel"
        ? libraryPath === origin.value
        : origin.kind === "loaded" && folders.length === 1 && samePath(folders[0], libraryPath);
    if (!ours) return;
    const filled = next.locations[0];
    pathOrigin.current = { kind: "panel", value: filled };
    setLibraryPath(filled);
  }

  function handleSave() {
    // Clear any prior success line and stale test result before a new attempt,
    // so at most one outcome (this save's) is ever on screen — never a green
    // "saved"/"connected" next to a fresh red failure.
    setStatusMsg("");
    test.reset();
    const body: components["schemas"]["PlexSettingsUpdate"] = {
      base_url: baseUrl,
      // Saved TRIMMED, because every check above compares it trimmed: a path
      // with a stray space passes `pathInside` and draws no warning, then goes
      // to the server padded, where `translate_path` joins it verbatim and
      // rebases every track onto " /musicdrop/..." — so the panel would report
      // all-clear on the one setting whose silent failure cost this app years.
      // A whitespace-only value is likewise saved as the empty string it looks
      // like, since blank means "same mount" to the backend and " " does not.
      library_path: libraryPath.trim(),
      library_section: librarySection,
    };
    // Omit the token unless the user typed a replacement, so a blank field
    // keeps the already-saved one (the API never returns it to prefill).
    const trimmed = token.trim();
    if (trimmed) body.token = trimmed;
    save.mutate(body, {
      onSuccess: () => {
        setToken("");
        setStatusMsg("Plex settings saved.");
      },
    });
  }

  function handleTest() {
    // Clear a prior save/test success line AND a prior save failure so this
    // test's outcome stands alone (never a red "couldn't save" beside a green).
    setStatusMsg("");
    save.reset();
    test.mutate(undefined, {
      onSuccess: (connection) => {
        if (connection.ok) {
          setStatusMsg(`Connected to ${connection.server_name ?? "Plex"}.`);
        }
      },
    });
  }

  const result = test.data;

  return (
    <>
      <div className="flex flex-col gap-4">
        <div className="flex flex-col gap-1">
          <label htmlFor="plex-base-url" className="text-sm font-medium">
            Base URL
          </label>
          <Input
            id="plex-base-url"
            placeholder="http://plex:32400"
            value={baseUrl}
            onChange={(e) => setBaseUrl(e.target.value)}
            className="max-w-md font-mono"
          />
        </div>

        <div className="flex flex-col gap-1">
          <label htmlFor="plex-token" className="text-sm font-medium">
            Admin token
          </label>
          <Input
            id="plex-token"
            type="password"
            // Stop browsers/password managers from autofilling a login password
            // or prompting to save this server secret. "new-password" is honored
            // more reliably than "off" for password inputs.
            autoComplete="new-password"
            placeholder={initial.has_token ? "Token saved. Enter to replace" : "X-Plex-Token"}
            value={token}
            onChange={(e) => setToken(e.target.value)}
            className="max-w-md font-mono"
          />
        </div>

        <div className="flex flex-col gap-1">
          <label htmlFor="plex-library-path" className="text-sm font-medium">
            Library path
          </label>
          <Input
            id="plex-library-path"
            placeholder="/data/music"
            value={libraryPath}
            onChange={(e) => {
              // Any edit makes the value the user's, so no later section pick
              // will overwrite it (see PathOrigin).
              pathOrigin.current = { kind: "typed" };
              setLibraryPath(e.target.value);
            }}
            className="max-w-md font-mono"
          />
          <p className="text-muted-foreground text-xs">
            music library path as Plex sees it; leave blank if the same
          </p>
          {/* One always-mounted polite region for what Plex reports about the
              selected library. Kept mounted (rather than inserted when a
              mismatch appears) so its text changes are announced — the same
              reason the save/test status line below is always mounted. It must
              not be display:none while idle either: a region hidden at the
              moment it gains content is not exposed, which would put us back to
              announcing on insertion. Empty, it is simply a zero-height box. */}
          <div role="status" aria-live="polite" className="flex max-w-md flex-col gap-1 text-xs">
            {selectedSection && folders.length > 0 && (
              <p className="text-muted-foreground">
                Plex reports {folders.length === 1 ? "this folder" : "these folders"} for “
                {selectedSection.title || "your music library"}”:{" "}
                {folders.map((folder, i) => (
                  <Fragment key={folder}>
                    {i > 0 && ", "}
                    {/* break-all: a path is an opaque string, and one long
                        segment with no "/" to wrap at pushed the whole page
                        sideways at 375px. */}
                    <span className="text-foreground font-mono break-all">{folder}</span>
                  </Fragment>
                ))}
              </p>
            )}
            {pathMismatch && (
              <p className="border-warning/50 bg-warning/10 text-foreground flex items-start gap-2 rounded-md border p-2">
                <Warning className="text-warning mt-0.5 size-4 shrink-0" aria-hidden="true" />
                <span>
                  Plex doesn’t list this path for that library, nor any folder that
                  contains it. Path matching will fail silently and every sync will fall
                  back to matching on tags — artist and title, then album, title and
                  length — which can land a track on a different copy of it.
                </span>
              </p>
            )}
            {folders.length > 0 && pathIsBlank && (
              <p className="text-muted-foreground">
                Blank passes paths through unchanged, which is right only if MusicDrop’s
                own library root is one of the folders above, or sits inside one. Nothing
                here can check that, so nothing is filled in for you: if Plex reaches your
                music by a different path, type that path.
              </p>
            )}
          </div>
        </div>

        <div className="flex flex-col gap-1">
          <label htmlFor="plex-library-section" className="text-sm font-medium">
            Library section
          </label>
          <select
            id="plex-library-section"
            className="border-input bg-background h-9 max-w-md rounded-md border px-2 text-sm"
            value={librarySection}
            onChange={(e) => handleSectionChange(e.target.value)}
          >
            <option value="">Auto: the only music library</option>
            {sectionOptions.map((title) => (
              <option key={title} value={title}>
                {title}
              </option>
            ))}
          </select>
          <p className="text-muted-foreground text-xs">
            which Plex music library playlists sync into; Auto works only if
            Plex has exactly one
          </p>
          {/* Always-mounted polite region, for the same reason as the folder
              one above: a region inserted along with its text is often missed. */}
          <div role="status" aria-live="polite" className="max-w-md text-xs">
            {autoIsAmbiguous && (
              <p className="border-warning/50 bg-warning/10 text-foreground flex items-start gap-2 rounded-md border p-2">
                <Warning className="text-warning mt-0.5 size-4 shrink-0" aria-hidden="true" />
                <span>
                  Plex has {fetchedSections.length} music libraries, so Auto can’t choose
                  between them. Until you pick one, every sync fails with “Multiple Plex
                  music libraries found.”
                </span>
              </p>
            )}
          </div>
        </div>
      </div>

      <div className="border-border flex flex-wrap items-center gap-3 border-t pt-4">
        <Button onClick={handleSave} disabled={save.isPending}>
          {save.isPending ? (
            <>
              <Spinner className="size-4 animate-spin" aria-hidden="true" />
              Saving…
            </>
          ) : (
            "Save"
          )}
        </Button>
        <Button
          variant="outline"
          onClick={handleTest}
          disabled={test.isPending || dirty}
        >
          {test.isPending ? (
            <>
              <Spinner className="size-4 animate-spin" aria-hidden="true" />
              Testing…
            </>
          ) : (
            "Test connection"
          )}
        </Button>

        {dirty && (
          <p className="text-muted-foreground text-xs">
            Save before testing. Test uses your saved settings.
          </p>
        )}

        {/* One always-mounted polite live region for save/test success. A
            region populated on insertion is often missed by screen readers, so
            we keep it mounted and only set its text — styled visible (success)
            when there is a message, sr-only when idle. Errors are separate
            role="alert" regions (announced on insertion by design). */}
        <p
          role="status"
          aria-live="polite"
          className={statusMsg ? "text-success flex items-center gap-1 text-sm" : "sr-only"}
        >
          {statusMsg ? (
            <>
              <Success className="size-4" aria-hidden="true" />
              {statusMsg}
            </>
          ) : null}
        </p>

        {save.isError && (
          <p className="text-destructive text-sm" role="alert">
            Couldn’t save Plex settings. Try again.
          </p>
        )}
        {result && !result.ok && (
          <p className="text-destructive text-sm" role="alert">
            {result.error ?? "Couldn’t reach Plex."}
          </p>
        )}
        {test.isError && (
          <p className="text-destructive text-sm" role="alert">
            Connection test failed. Check the URL and token.
          </p>
        )}
      </div>
    </>
  );
}

/** Section shell: SettingsSection provides the section-scale h2 + the bordered
 * panel, so the old heading-over-Card sandwich collapses to one box. */
function Panel({ children }: { children: React.ReactNode }) {
  return (
    <SettingsSection
      title="Plex"
      description="Connect your Plex server so playlists can be pushed to it. The admin token is write-only; it’s stored on the server and never shown again."
    >
      {children}
    </SettingsSection>
  );
}
