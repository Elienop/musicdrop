import { CheckCircle2, Loader2 } from "lucide-react";
import { useState } from "react";

import {
  type PlexSettings,
  usePlexSettings,
  useSavePlexSettings,
  useTestPlex,
} from "@/api/usePlex";
import type { components } from "@/api/schema";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
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
  // Remount on a fresh snapshot (post-save) so the inputs reseed from disk.
  const d = settings.data;
  return <PlexSettingsEditor key={`${d.base_url}|${d.library_path}|${d.has_token}`} initial={d} />;
}

function PlexSettingsEditor({ initial }: { initial: PlexSettings }) {
  const save = useSavePlexSettings();
  const test = useTestPlex();

  const [baseUrl, setBaseUrl] = useState(initial.base_url);
  const [token, setToken] = useState("");
  const [libraryPath, setLibraryPath] = useState(initial.library_path);

  function handleSave() {
    const body: components["schemas"]["PlexSettingsUpdate"] = {
      base_url: baseUrl,
      library_path: libraryPath,
    };
    // Omit the token unless the user typed a replacement, so a blank field
    // keeps the already-saved one (the API never returns it to prefill).
    const trimmed = token.trim();
    if (trimmed) body.token = trimmed;
    save.mutate(body, { onSuccess: () => setToken("") });
  }

  const result = test.data;

  return (
    <Panel>
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
        <Button variant="outline" onClick={() => test.mutate()} disabled={test.isPending}>
          {test.isPending ? (
            <>
              <Loader2 className="size-4 animate-spin" aria-hidden="true" />
              Testing…
            </>
          ) : (
            "Test connection"
          )}
        </Button>

        {save.isError && (
          <p className="text-destructive text-sm" role="alert">
            Couldn’t save Plex settings. Try again.
          </p>
        )}

        {result?.ok && (
          <p className="text-sm text-green-700 dark:text-green-400" role="status">
            <CheckCircle2 className="mr-1 inline size-4" aria-hidden="true" />
            Connected to {result.server_name ?? "Plex"}.
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
    </Panel>
  );
}

function Panel({ children }: { children: React.ReactNode }) {
  return (
    <Card aria-label="Plex">
      <CardHeader>
        <CardTitle className="text-2xl tracking-tight">Plex</CardTitle>
        <CardDescription>
          Connect your Plex server so playlists can be pushed to it. The admin token is
          write-only — it’s stored on the server and never shown again.
        </CardDescription>
      </CardHeader>
      {children}
    </Card>
  );
}
