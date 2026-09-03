import { type SubmitEvent, useRef, useState } from "react";

import { useAuthStatus, useChangePassword } from "@/api/auth";
import { Spinner, Success } from "@/components/icons";
import { SettingsSection } from "@/components/system/SettingsSection";
import { StatusBanner } from "@/components/system/StatusBanner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

/** Said after a successful change. The second sentence is not a warning — it is
 * the only revocation this session design has, so an operator who suspects a
 * cookie has leaked needs to know that changing the password is what clears it.
 * The signing key is derived from the password hash, so every OTHER cookie
 * stops verifying on its next request (`backend/app/auth/session.py`); this
 * browser's own is re-minted by the same response. */
const CHANGED_MESSAGE =
  "Password changed. Every other signed-in browser was signed out; this one stays in.";

/** The mismatch sentence, checked here so a typo costs no scrypt derive — the
 * route takes one new password and cannot see a confirmation field. Worded the
 * same as the sign-in screen's setup form, because it is the same mistake. */
const MISMATCH_MESSAGE = "The two passwords don’t match. Type them again.";

/** Ties the rejection to the fields, for `aria-describedby`. Static ids: this
 * panel is mounted once per route. */
const ERROR_ID = "account-password-error";

/**
 * Settings → Account: change the single password this server is protected by.
 *
 * The panel asks `GET /api/auth/status` for `password_source` rather than
 * assuming a form is useful. Under the environment override the route refuses
 * with a 409 no matter what is typed, so a form here would be a dead control —
 * and hiding the panel outright would leave the operator with no way to find
 * out WHY the app will not manage their password.
 */
export function AccountPanel() {
  const status = useAuthStatus();

  if (status.isPending) {
    return (
      <Panel>
        <output className="text-muted-foreground block text-sm">
          Checking how this server’s password is configured…
        </output>
      </Panel>
    );
  }
  if (status.isError || !status.data) {
    return (
      <Panel>
        <div className="flex flex-col items-start gap-2">
          <p className="text-destructive text-sm" role="alert">
            Couldn’t check how this server’s password is configured.
          </p>
          <Button variant="outline" size="sm" onClick={() => void status.refetch()}>
            Try again
          </Button>
        </div>
      </Panel>
    );
  }
  if (status.data.password_source === "env") {
    return (
      <Panel>
        <EnvOverrideNotice />
      </Panel>
    );
  }
  return (
    <Panel>
      <ChangePasswordForm />
    </Panel>
  );
}

/** The panel shell. Its description carries the forgotten-password recovery,
 * because this is the screen an operator looks at while they still CAN sign in
 * — the sign-in screen can only say it once they cannot. */
function Panel({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <SettingsSection
      title="Password"
      description="MusicDrop is protected by one password, shared by everyone who uses this server. If you forget it, delete the password-hash file in MusicDrop’s beets directory and restart MusicDrop — the sign-in screen will then set a new one."
    >
      {children}
    </SettingsSection>
  );
}

/**
 * `MUSICDROP_PASSWORD_HASH` is set, so it is the live password and the stored
 * one (if any) is shadowed. A neutral notice rather than a warning: nothing is
 * broken here, the app simply is not the owner of this credential.
 *
 * Mirrors `backend/app/api/auth.py::_ENV_OVERRIDE_DETAIL`, which is what the
 * route answers if a form is submitted anyway.
 */
function EnvOverrideNotice() {
  return (
    <StatusBanner tone="neutral">
      This server’s password comes from{" "}
      <code className="font-mono">MUSICDROP_PASSWORD_HASH</code>, which
      overrides any password stored by the app, so it can’t be changed here.
      Unset that variable and restart MusicDrop to hand the password over to the
      app; the sign-in screen will then set a new one.
    </StatusBanner>
  );
}

/** Current / new / confirm, and the four answers the route can give back. */
function ChangePasswordForm() {
  const change = useChangePassword();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [mismatch, setMismatch] = useState(false);
  // Always-mounted polite region (the Plex/slskd dialect): a live region that
  // is inserted at the moment it gains text is often not announced at all, so
  // the success sentence is state rather than a conditionally mounted node.
  const [changed, setChanged] = useState(false);
  const currentRef = useRef<HTMLInputElement>(null);

  function handleSubmit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    setChanged(false);
    if (next !== confirm) {
      setMismatch(true);
      return;
    }
    setMismatch(false);
    change.mutate(
      { currentPassword: current, newPassword: next },
      {
        onSuccess: () => {
          // Clear all three: the old password is now wrong and the new one is
          // saved, so every field on screen holds a stale secret.
          setCurrent("");
          setNext("");
          setConfirm("");
          setChanged(true);
        },
        onError: (error) => {
          // A wrong current password is the one failure the user fixes by
          // typing again, and submitting disabled the button — which drops
          // focus to <body>. Send it back to the field that was wrong.
          if (error.status === 403) {
            currentRef.current?.focus();
            currentRef.current?.select();
          }
        },
      },
    );
  }

  // A 409 says the change is impossible in this server's state (the env
  // override is live, or there is nothing stored to verify against) — not that
  // the user typed something wrong. It reads as a notice, not as a field error.
  // Unreachable while the panel hides the form under "env", and handled anyway:
  // the status this panel read is a cached answer, and a restart can invalidate
  // it under an open tab.
  const conflict = change.error?.status === 409 ? change.error.message : null;
  const rejection = conflict === null ? change.error?.message : undefined;
  const shown = mismatch ? MISMATCH_MESSAGE : rejection;

  return (
    <form onSubmit={handleSubmit} className="flex max-w-md flex-col gap-4">
      <div className="flex flex-col gap-1">
        <label htmlFor="account-current-password" className="text-sm font-medium">
          Current password
        </label>
        <Input
          id="account-current-password"
          type="password"
          autoComplete="current-password"
          required
          ref={currentRef}
          aria-invalid={change.error?.status === 403}
          aria-describedby={shown === undefined ? undefined : ERROR_ID}
          value={current}
          onChange={(e) => setCurrent(e.target.value)}
        />
      </div>
      <div className="flex flex-col gap-1">
        <label htmlFor="account-new-password" className="text-sm font-medium">
          New password
        </label>
        <Input
          id="account-new-password"
          type="password"
          autoComplete="new-password"
          required
          value={next}
          onChange={(e) => setNext(e.target.value)}
        />
      </div>
      <div className="flex flex-col gap-1">
        <label
          htmlFor="account-confirm-password"
          className="text-sm font-medium"
        >
          Confirm new password
        </label>
        <Input
          id="account-confirm-password"
          type="password"
          autoComplete="new-password"
          required
          aria-invalid={mismatch}
          aria-describedby={shown === undefined ? undefined : ERROR_ID}
          value={confirm}
          onChange={(e) => {
            setMismatch(false);
            setConfirm(e.target.value);
          }}
        />
      </div>
      <Button type="submit" disabled={change.isPending} className="self-start">
        {change.isPending && (
          <Spinner className="size-4 animate-spin" aria-hidden="true" />
        )}
        {change.isPending ? "Changing…" : "Change password"}
      </Button>
      <p
        role="status"
        aria-live="polite"
        className={
          changed ? "text-success flex items-center gap-1 text-sm" : "sr-only"
        }
      >
        {changed ? (
          <>
            <Success className="size-4" aria-hidden="true" />
            {CHANGED_MESSAGE}
          </>
        ) : null}
      </p>
      {conflict !== null && <StatusBanner tone="neutral">{conflict}</StatusBanner>}
      {/* Rendered verbatim: the server names its own cause for each of these —
          a wrong current password (403), a blank new one (422), a derive
          already running (429), a data directory it cannot write (503) — and a
          re-worded copy here could only drift from it. The mismatch above is
          the one sentence this side owns. */}
      {shown !== undefined && (
        <p id={ERROR_ID} className="text-destructive text-sm" role="alert">
          {shown}
        </p>
      )}
    </form>
  );
}
