import { Check, CheckCircle2, Copy, Loader2 } from "lucide-react";
import { useEffect, useState } from "react";

import {
  type SlskdSettings,
  type SlskdSettingsUpdate,
  useSaveSlskdSettings,
  useSlskdSettings,
  useTestSlskd,
} from "@/api/useSlskd";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardFooter } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";

/** The paste-in slskd config that points its completion webhook back at this
 * app. Read-only + copyable — slskd posts to `/api/slskd/webhook` on every
 * finished download, authenticating with the webhook secret set below. */
const WEBHOOK_SNIPPET = `integration:
  webhooks:
    musicdrop:
      on: [DownloadDirectoryComplete]
      call:
        url: http://<musicdrop-host>:3030/api/slskd/webhook
        headers:
          - name: X-API-Key
            value: <your webhook secret>`;

/** Settings → slskd: connect a slskd instance (base URL + write-only API key +
 * the downloads path as slskd sees it), set the shared webhook secret, and flip
 * auto-import so a completed download imports itself. Both secrets are
 * write-only — the API returns only `has_token` / `has_webhook_secret`, so each
 * field shows a "saved" placeholder and is sent only when the user types a
 * replacement. */
export function SlskdPanel() {
  const settings = useSlskdSettings();

  if (settings.isPending) {
    return (
      <Panel>
        <CardContent>
          <p className="text-muted-foreground text-sm" role="status">
            Loading slskd settings…
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
            Could not load slskd settings.
          </p>
        </CardContent>
      </Panel>
    );
  }
  return (
    <Panel>
      <SlskdSettingsEditor initial={settings.data} />
    </Panel>
  );
}

function SlskdSettingsEditor({ initial }: { initial: SlskdSettings }) {
  const save = useSaveSlskdSettings();
  const test = useTestSlskd();

  const [baseUrl, setBaseUrl] = useState(initial.base_url);
  const [token, setToken] = useState("");
  const [downloadsPrefix, setDownloadsPrefix] = useState(
    initial.downloads_prefix,
  );
  const [webhookSecret, setWebhookSecret] = useState("");
  const [autoImport, setAutoImport] = useState(initial.auto_import);
  // Drives the single, always-mounted polite live region below (see the Plex
  // panel): a live region must already exist when its text changes to be
  // reliably announced, so the save/test handlers set this rather than
  // conditionally mounting the confirmation node.
  const [statusMsg, setStatusMsg] = useState("");
  const [copied, setCopied] = useState(false);

  // Reseed the inputs when the persisted snapshot changes (e.g. after a Save
  // refetch) WITHOUT remounting — a remount-on-key would strand keyboard focus
  // on <body>, most visibly on the first secret save which flips has_token /
  // has_webhook_secret. Keyed on the snapshot's field identity (incl. both
  // has_* flags) so an unchanged background refetch leaves an edit alone.
  useEffect(() => {
    setBaseUrl(initial.base_url);
    setDownloadsPrefix(initial.downloads_prefix);
    setAutoImport(initial.auto_import);
    setToken("");
    setWebhookSecret("");
  }, [
    initial.base_url,
    initial.downloads_prefix,
    initial.auto_import,
    initial.has_token,
    initial.has_webhook_secret,
  ]);

  // The form differs from what's saved on the server. "Test connection" probes
  // the SAVED config (not the typed-but-unsaved values), so testing a dirty form
  // would silently test stale settings — disable Test until the user Saves.
  const dirty =
    baseUrl !== initial.base_url ||
    downloadsPrefix !== initial.downloads_prefix ||
    autoImport !== initial.auto_import ||
    token.trim().length > 0 ||
    webhookSecret.trim().length > 0;

  function handleSave() {
    const body: SlskdSettingsUpdate = {
      base_url: baseUrl,
      downloads_prefix: downloadsPrefix,
      auto_import: autoImport,
    };
    // Omit each secret unless the user typed a replacement, so a blank field
    // keeps the already-saved one (the API never returns it to prefill).
    const trimmedToken = token.trim();
    if (trimmedToken) body.token = trimmedToken;
    const trimmedSecret = webhookSecret.trim();
    if (trimmedSecret) body.webhook_secret = trimmedSecret;
    save.mutate(body, {
      onSuccess: () => {
        setToken("");
        setWebhookSecret("");
        setStatusMsg("slskd settings saved.");
      },
    });
  }

  function handleTest() {
    test.mutate(undefined, {
      onSuccess: (connection) => {
        if (connection.ok) {
          setStatusMsg(
            connection.version
              ? `Connected to slskd ${connection.version}.`
              : "Connected to slskd.",
          );
        }
      },
    });
  }

  async function handleCopy() {
    try {
      await navigator.clipboard.writeText(WEBHOOK_SNIPPET);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard access can be denied (insecure context / permission); the
      // snippet stays visible to select manually, so a failed copy is a no-op.
    }
  }

  const result = test.data;

  return (
    <>
      <CardContent className="flex flex-col gap-4">
        <div className="flex flex-col gap-1">
          <label htmlFor="slskd-base-url" className="text-sm font-medium">
            Base URL
          </label>
          <Input
            id="slskd-base-url"
            placeholder="http://slskd:5030"
            value={baseUrl}
            onChange={(e) => setBaseUrl(e.target.value)}
            className="max-w-md font-mono"
          />
        </div>

        <div className="flex flex-col gap-1">
          <label htmlFor="slskd-token" className="text-sm font-medium">
            API key
          </label>
          <Input
            id="slskd-token"
            type="password"
            // Stop browsers/password managers from autofilling a login password
            // or prompting to save this server secret.
            autoComplete="new-password"
            placeholder={
              initial.has_token
                ? "API key saved — enter to replace"
                : "slskd API key"
            }
            value={token}
            onChange={(e) => setToken(e.target.value)}
            className="max-w-md font-mono"
          />
        </div>

        <div className="flex flex-col gap-1">
          <label
            htmlFor="slskd-downloads-prefix"
            className="text-sm font-medium"
          >
            Downloads path
          </label>
          <Input
            id="slskd-downloads-prefix"
            placeholder="/downloads"
            value={downloadsPrefix}
            onChange={(e) => setDownloadsPrefix(e.target.value)}
            className="max-w-md font-mono"
          />
          <p className="text-muted-foreground text-xs">
            slskd&rsquo;s download root, as slskd sees it — stripped when a
            completed drop is mapped into the inbox
          </p>
        </div>

        <div className="flex flex-col gap-1">
          <label htmlFor="slskd-webhook-secret" className="text-sm font-medium">
            Webhook secret
          </label>
          <Input
            id="slskd-webhook-secret"
            type="password"
            autoComplete="new-password"
            placeholder={
              initial.has_webhook_secret
                ? "Secret saved — enter to replace"
                : "shared webhook secret"
            }
            value={webhookSecret}
            onChange={(e) => setWebhookSecret(e.target.value)}
            className="max-w-md font-mono"
          />
          <p className="text-muted-foreground text-xs">
            the shared secret slskd sends as{" "}
            <code className="font-mono">X-API-Key</code> on each completion
            webhook
          </p>
        </div>

        <div className="flex items-start gap-3">
          <Switch
            id="slskd-auto-import"
            checked={autoImport}
            onCheckedChange={setAutoImport}
          />
          <div className="flex flex-col gap-1">
            <label htmlFor="slskd-auto-import" className="text-sm font-medium">
              Auto-import completed downloads
            </label>
            <p className="text-muted-foreground text-xs">
              when on, a finished slskd download imports itself into the
              library; uncertain matches are set aside for review
            </p>
          </div>
        </div>

        <div className="flex flex-col gap-2">
          <div className="flex items-center justify-between gap-2">
            <p className="text-sm font-medium">Webhook configuration</p>
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={handleCopy}
            >
              {copied ? (
                <>
                  <Check className="size-4" aria-hidden="true" />
                  Copied
                </>
              ) : (
                <>
                  <Copy className="size-4" aria-hidden="true" />
                  Copy
                </>
              )}
            </Button>
          </div>
          <p className="text-muted-foreground text-xs">
            Add this to slskd&rsquo;s config so it notifies MusicDrop when a
            download finishes (use the webhook secret you set above).
          </p>
          <pre className="bg-muted overflow-x-auto rounded-lg p-3 font-mono text-xs">
            {WEBHOOK_SNIPPET}
          </pre>
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
          title={
            dirty
              ? "Save before testing — Test uses your saved settings"
              : undefined
          }
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

        {/* One always-mounted polite live region for save/test success (see the
            Plex panel): kept mounted, text-only updates, sr-only when idle. */}
        <p
          role="status"
          aria-live="polite"
          className={
            statusMsg
              ? "text-success flex items-center gap-1 text-sm"
              : "sr-only"
          }
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
            Couldn’t save slskd settings. Try again.
          </p>
        )}
        {result && !result.ok && (
          <p className="text-destructive text-sm" role="alert">
            {result.error ?? "Couldn’t reach slskd."}
          </p>
        )}
        {test.isError && (
          <p className="text-destructive text-sm" role="alert">
            Connection test failed. Check the URL and API key.
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
    <section
      aria-labelledby="slskd-settings-heading"
      className="flex flex-col gap-4"
    >
      <header className="flex flex-col gap-1">
        <h2
          id="slskd-settings-heading"
          className="text-2xl font-semibold tracking-tight"
        >
          slskd
        </h2>
        <p className="text-muted-foreground text-sm">
          Connect slskd so completed Soulseek downloads import themselves into
          the library. The API key and webhook secret are write-only — stored on
          the server and never shown again.
        </p>
      </header>
      <Card>{children}</Card>
    </section>
  );
}
