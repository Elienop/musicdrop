import { useEffect, useState } from "react";
import { Link } from "react-router";

import {
  IMPORT_OPERATION_LINE,
  useImportOperation,
} from "@/api/useImportOperation";
import {
  type SlskdSettings,
  type SlskdSettingsUpdate,
  useSaveSlskdSettings,
  useSlskdSettings,
  useTestSlskd,
} from "@/api/useSlskd";
import { Spinner, Success, Warning } from "@/components/icons";
import { CopyableSnippet } from "@/components/system/CopyableSnippet";
import { SettingsSection } from "@/components/system/SettingsSection";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";

/** The paste-in slskd config that points its completion webhook back at this
 * app. Read-only + copyable — slskd posts to `/api/slskd/webhook` on every
 * finished download, authenticating with the webhook secret set below.
 * `retry.attempts` is slskd's own setting (it tries once by default); it
 * re-sends a webhook MusicDrop missed while restarting. */
const WEBHOOK_SNIPPET = `integration:
  webhooks:
    musicdrop:
      on: [DownloadDirectoryComplete]
      call:
        url: http://<musicdrop-host>:3030/api/slskd/webhook  # host must be an IP, or a name listed in MUSICDROP_ALLOWED_HOSTS
        headers:
          - name: X-API-Key
            value: <your webhook secret>
      retry:
        attempts: 10`;

/** The auto-import switch's help, and the line under it saying what an import
 * does with the downloaded files. */
const AUTO_IMPORT_HELP_ID = "slskd-auto-import-help";
const FILES_LINE_ID = "slskd-files-help";

/** Settings → slskd: connect a slskd instance (base URL + write-only API key +
 * Path in slskd, slskd's download folder as slskd sees it), set the shared webhook secret, and flip
 * auto-import so a completed download imports itself. Both secrets are
 * write-only — the API returns only `has_token` / `has_webhook_secret`, so each
 * field shows a "saved" placeholder and is sent only when the user types a
 * replacement. */
export function SlskdPanel() {
  const settings = useSlskdSettings();

  if (settings.isPending) {
    return (
      <Panel>
        <output className="text-muted-foreground text-sm block">
          Loading slskd settings…
        </output>
      </Panel>
    );
  }
  if (settings.isError || !settings.data) {
    return (
      <Panel>
        <p className="text-destructive text-sm" role="alert">
          Could not load slskd settings.
        </p>
      </Panel>
    );
  }
  return (
    <Panel>
      <SlskdSettingsEditor initial={settings.data} />
    </Panel>
  );
}

function SlskdSettingsEditor({ initial }: Readonly<{ initial: SlskdSettings }>) {
  const save = useSaveSlskdSettings();
  const test = useTestSlskd();
  // slskd downloads import with beets' own file operation, as every import
  // does; Add from folder says it under its path box in the same words.
  const operation = useImportOperation();
  const filesLine =
    operation.data === undefined ? null : IMPORT_OPERATION_LINE[operation.data];

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
    // Clear any prior success line and stale test result before a new attempt,
    // so at most one outcome (this save's) is ever on screen.
    setStatusMsg("");
    test.reset();
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
    // Clear a prior save/test success line AND a prior save failure so this
    // test's outcome stands alone (never a red "couldn't save" beside a green).
    setStatusMsg("");
    save.reset();
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

  const result = test.data;

  return (
    <>
      <div className="flex flex-col gap-4">
        <div className="flex flex-col gap-1">
          <label htmlFor="slskd-base-url" className="text-sm font-medium">
            Base URL
          </label>
          <Input
            id="slskd-base-url"
            placeholder="http://127.0.0.1:5030"
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
                ? "API key saved. Enter to replace"
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
            Path in slskd
          </label>
          <Input
            id="slskd-downloads-prefix"
            placeholder="Same as Folder"
            value={downloadsPrefix}
            onChange={(e) => setDownloadsPrefix(e.target.value)}
            aria-describedby={
              initial.last_download_missed
                ? "slskd-downloads-prefix-help slskd-downloads-prefix-missed"
                : "slskd-downloads-prefix-help"
            }
            className="max-w-md font-mono"
          />
          <p
            id="slskd-downloads-prefix-help"
            className="text-muted-foreground text-xs"
          >
            slskd&rsquo;s download folder
            {" ("}<code className="font-mono">directories.downloads</code>), as
            slskd sees it.
          </p>
          {/* Set by the server when the last slskd webhook that reached the
              mapping named a folder outside slskd's folder here; cleared by
              the next one that maps. A remembered state, not an event, so no
              alert role: it matches the Review page's "Last error" line. */}
          {initial.last_download_missed && (
            <p
              id="slskd-downloads-prefix-missed"
              className="text-destructive flex items-start gap-2 text-sm"
            >
              <Warning className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
              <span className="min-w-0 break-words">
                Last download didn&rsquo;t match Path in slskd.
              </span>
            </p>
          )}
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
                ? "Secret saved. Enter to replace"
                : "shared webhook secret"
            }
            value={webhookSecret}
            onChange={(e) => setWebhookSecret(e.target.value)}
            className="max-w-md font-mono"
          />
          <p className="text-muted-foreground text-xs">
            The secret slskd sends as{" "}
            <code className="font-mono">X-API-Key</code> with each webhook.
          </p>
        </div>

        <div className="flex items-start gap-3">
          <Switch
            id="slskd-auto-import"
            checked={autoImport}
            onCheckedChange={setAutoImport}
            aria-describedby={
              filesLine === null
                ? AUTO_IMPORT_HELP_ID
                : `${AUTO_IMPORT_HELP_ID} ${FILES_LINE_ID}`
            }
          />
          <div className="flex flex-col gap-1">
            <label htmlFor="slskd-auto-import" className="text-sm font-medium">
              Auto-import completed downloads
            </label>
            <p id={AUTO_IMPORT_HELP_ID} className="text-muted-foreground text-xs">
              When on, a finished slskd download imports itself into the
              library; uncertain matches are set aside for review.
            </p>
            {/* What an import does with the files, from the operation beets
                loaded. Nothing while it loads or if it can't be read: a guess
                would be a promise about the user's files. The id is on the
                sentence only, so the switch is not described as "… Change". */}
            {filesLine !== null && (
              <p className="text-muted-foreground text-xs">
                <span id={FILES_LINE_ID}>{filesLine}</span>{" "}
                <Link
                  to="/settings/beets"
                  className="text-foreground focus-ring rounded-sm underline"
                >
                  Change
                </Link>
              </p>
            )}
          </div>
        </div>

        <CopyableSnippet label="Webhook configuration" snippet={WEBHOOK_SNIPPET}>
          <p className="text-muted-foreground text-xs">
            Add this to slskd&rsquo;s config so it notifies MusicDrop when a
            download finishes (use the webhook secret you set above).
          </p>
        </CopyableSnippet>

        <p className="text-muted-foreground border-t pt-4 text-sm">
          Set-aside downloads and imports needing a decision appear in{" "}
          <Link
            to="/review"
            className="text-foreground focus-ring rounded-sm underline"
          >
            Review
          </Link>
          .
        </p>
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
              <Success className="size-4" aria-hidden="true" />
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
      </div>
    </>
  );
}

/** Section shell: SettingsSection provides the section-scale h2 + the bordered
 * panel, so the old heading-over-Card sandwich collapses to one box. */
function Panel({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <SettingsSection
      title="slskd"
      description="Connect slskd so completed Soulseek downloads import themselves into the library. The API key and webhook secret are write-only; stored on the server and never shown again."
    >
      {children}
    </SettingsSection>
  );
}
