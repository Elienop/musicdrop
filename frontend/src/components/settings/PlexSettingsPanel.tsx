import { CheckCircle2, Loader2 } from "lucide-react";
import { useEffect, useState } from "react";

import {
  type PlexSettings,
  usePlexSettings,
  useSavePlexSettings,
  useTestPlex,
} from "@/api/usePlex";
import type { components } from "@/api/schema";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardFooter } from "@/components/ui/card";
import { Input } from "@/components/ui/input";

/** Settings → Plex: the single-account connection (base URL + write-only admin
 * token + the music-library path as Plex sees it) plus a connection test. The
 * token is write-only — the API returns only `has_token`, so the field shows a
 * "saved" placeholder and is sent only when the user types a replacement. */
export function PlexSettingsPanel() {
  const settings = usePlexSettings();

  if (settings.isPending) {
    return (
      <Panel>
        <CardContent>
          <p className="text-muted-foreground text-sm" role="status">
            Loading Plex settings…
          </p>
        </CardContent>
      </Panel>
    );
  }
  if (settings.isError || !settings.data) {
    return (
      <Panel>
        <CardContent>
          <p className="text-destructive text-sm" role="alert">
            Could not load Plex settings.
          </p>
        </CardContent>
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

  const [baseUrl, setBaseUrl] = useState(initial.base_url);
  const [token, setToken] = useState("");
  const [libraryPath, setLibraryPath] = useState(initial.library_path);
  // Drives the single, always-mounted polite live region below. A live region
  // must already exist when its text changes to be reliably announced, so we
  // set this state from the save/test handlers rather than conditionally
  // mounting the confirmation nodes.
  const [statusMsg, setStatusMsg] = useState("");

  // Reseed the inputs when the persisted snapshot changes (e.g. after a Save
  // refetch) WITHOUT remounting. A remount-on-key would destroy the focused
  // Save button and strand keyboard focus on <body> — most visibly on the first
  // token save, which flips `has_token`. Keyed on the snapshot's field identity
  // so an unchanged background refetch leaves an in-progress edit alone.
  useEffect(() => {
    setBaseUrl(initial.base_url);
    setLibraryPath(initial.library_path);
    setToken("");
  }, [initial.base_url, initial.library_path, initial.has_token]);

  // The form differs from what's saved on the server. "Test connection" probes
  // the SAVED config (not the typed-but-unsaved values), so testing a dirty form
  // would silently test stale settings — disable Test until the user Saves.
  const dirty =
    baseUrl !== initial.base_url ||
    libraryPath !== initial.library_path ||
    token.trim().length > 0;

  function handleSave() {
    const body: components["schemas"]["PlexSettingsUpdate"] = {
      base_url: baseUrl,
      library_path: libraryPath,
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
      <CardContent className="flex flex-col gap-4">
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
            placeholder={initial.has_token ? "Token saved — enter to replace" : "X-Plex-Token"}
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
            onChange={(e) => setLibraryPath(e.target.value)}
            className="max-w-md font-mono"
          />
          <p className="text-muted-foreground text-xs">
            music library path as Plex sees it; leave blank if the same
          </p>
        </div>
      </CardContent>

      <CardFooter className="flex flex-wrap items-center gap-3">
        <Button onClick={handleSave} disabled={save.isPending}>
          {save.isPending ? (
            <>
              <Loader2 className="size-4 animate-spin" aria-hidden="true" />
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
          title={dirty ? "Save before testing — Test uses your saved settings" : undefined}
        >
          {test.isPending ? (
            <>
              <Loader2 className="size-4 animate-spin" aria-hidden="true" />
              Testing…
            </>
          ) : (
            "Test connection"
          )}
        </Button>

        {dirty && (
          <p className="text-muted-foreground text-xs">
            Save before testing — Test uses your saved settings.
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
              <CheckCircle2 className="size-4" aria-hidden="true" />
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
      </CardFooter>
    </>
  );
}

/** Section shell matching the sibling Settings panels: a real <h2> heading (so
 * it shows up in heading navigation, unlike a shadcn CardTitle div) over a Card
 * holding the form. */
function Panel({ children }: { children: React.ReactNode }) {
  return (
    <section aria-labelledby="plex-settings-heading" className="flex flex-col gap-4">
      <header className="flex flex-col gap-1">
        <h2 id="plex-settings-heading" className="text-2xl font-semibold tracking-tight">
          Plex
        </h2>
        <p className="text-muted-foreground text-sm">
          Connect your Plex server so playlists can be pushed to it. The admin token is
          write-only — it’s stored on the server and never shown again.
        </p>
      </header>
      <Card>{children}</Card>
    </section>
  );
}
