import { type SubmitEvent, useRef, useState } from "react";

import { useAuthStatus, useChangePassword } from "@/api/auth";
import { Info, Spinner, Success } from "@/components/icons";
import { HiddenUsernameField } from "@/components/system/HiddenUsernameField";
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

/** The panel's lede: one line, the shape the sibling panels' descriptions take,
 * and the same vocabulary the sign-in card uses for the same fact ("a single
 * password", "the one everyone uses"). The recovery used to live here
 * and is a footnote under the form now — it is an aside, read once months
 * before it is needed, and in the lede it was the tallest thing in the panel on
 * a phone, sitting above three fields it is not about. */
const DESCRIPTION =
  "MusicDrop is protected by a single password, the one everyone uses to sign in to this server.";

/** The same lede, plus the fact that changes what the rest of the panel can
 * offer. The recovery footnote is NOT rendered under the override: "delete the
 * password-hash file" is not merely incomplete there — the variable wins
 * whether or not a file is present, so deleting one changes nothing, and a
 * locked-out operator would be sent to do the one thing that cannot help. The
 * recovery that IS true there is unsetting the variable, which the notice
 * says. */
const DESCRIPTION_UNDER_OVERRIDE =
  "MusicDrop is protected by a single password, the one everyone uses to sign in to this server. On this server it is set outside the app.";

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
        <output className="text-muted-foreground text-sm block">
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
      <Panel description={DESCRIPTION_UNDER_OVERRIDE}>
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

/** The panel shell. Its default description carries the forgotten-password
 * recovery, because this is the screen an operator looks at while they still CAN
 * sign in — the sign-in screen can only say it once they cannot. */
function Panel({
  children,
  description = DESCRIPTION,
}: Readonly<{ children: React.ReactNode; description?: string }>) {
  return (
    <SettingsSection title="Password" description={description}>
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
    // `icon` rather than prose alone: the neutral banners elsewhere in the app
    // carry a glyph, and a bg-muted box without one reads as a quoted
    // paragraph rather than as a notice from the system.
    <StatusBanner tone="neutral" icon={Info}>
      This server’s password comes from{" "}
      <code className="font-mono">MUSICDROP_PASSWORD_HASH</code>, which
      overrides any password stored by the app, so it can’t be changed here.
      Deleting the <code className="font-mono">password-hash</code> file won’t
      reset it either. To let the app manage the password, unset that variable
      and restart MusicDrop: if a password is stored on this server it applies
      again, otherwise the sign-in screen sets a new one.
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
  const confirmRef = useRef<HTMLInputElement>(null);

  function handleSubmit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    setChanged(false);
    if (next !== confirm) {
      setMismatch(true);
      // The sentence says "type them again", so put the caret where typing
      // goes — the same thing the sign-in screen's setup form does for the
      // same mistake, rather than leaving focus on the button that was clicked.
      confirmRef.current?.focus();
      confirmRef.current?.select();
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
          // Submitting disabled the button, which drops focus to <body>, and
          // the three fields were just emptied — so after a change that WORKED
          // there was nothing focused at all. The first field is where the form
          // starts; the polite region in the action row still announces it.
          currentRef.current?.focus();
        },
        onError: (error) => {
          // Submitting disabled the button, which drops focus to <body>, and
          // the rejections this form keeps rendering for (403, 422, 429, 503,
          // and a request with no answer) all leave it there. Send it back to
          // the first field; a wrong current password also SELECTS, being the
          // one of them about the value sitting in that field.
          currentRef.current?.focus();
          if (error.status === 403) {
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
    <form onSubmit={handleSubmit} className="flex flex-col gap-4">
      <HiddenUsernameField />
      <div className="flex flex-col gap-1">
        <label
          htmlFor="account-current-password"
          className="text-sm font-medium"
        >
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
          // The width cap belongs to each field, as it does in the Plex and
          // slskd panels; on the form it also capped the action row below.
          className="max-w-md"
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
          onChange={(e) => {
            // Clear on edit, from EITHER half of the pair: the mismatch was
            // about the two as they stood at submit, and retyping this one is
            // the likelier correction.
            setMismatch(false);
            setNext(e.target.value);
          }}
          className="max-w-md"
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
          ref={confirmRef}
          aria-invalid={mismatch}
          // The MISMATCH only. It is the one sentence about this pair; the
          // server's rejections are about the current password or about the
          // request, and pointing this field at them told a screen-reader user
          // a fact about a field they were not in.
          aria-describedby={mismatch ? ERROR_ID : undefined}
          value={confirm}
          onChange={(e) => {
            setMismatch(false);
            setConfirm(e.target.value);
          }}
          className="max-w-md"
        />
      </div>
      {/* The recovery, as a footnote under the fields (the slskd panel's
          shape): an aside read once, months before it is needed, rather than
          the panel's lede above three fields it is not about. */}
      <p className="text-muted-foreground border-t pt-4 text-sm">
        Forgot it? Delete the <code className="font-mono">password-hash</code>{" "}
        file in MusicDrop’s beets directory and restart MusicDrop. The sign-in
        screen will then set a new one.
      </p>
      {/* Above the action row and full width: it is about the server's state,
          not about the button. */}
      {conflict !== null && (
        <StatusBanner tone="neutral" icon={Info}>
          {conflict}
        </StatusBanner>
      )}
      {/* The action row the sibling settings panels end their forms with: the
          button and its feedback on one line, under a divider that separates
          them from the fields.

          Rendered verbatim: the server names its own cause for each rejection —
          a wrong current password (403), a blank new one (422), a derive
          already running (429), a data directory it cannot write (503) — and a
          re-worded copy here could only drift from it. The mismatch is the one
          sentence this side owns. */}
      <div className="border-border flex flex-wrap items-center gap-3 border-t pt-4">
        <Button type="submit" disabled={change.isPending}>
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
        {shown !== undefined && (
          <p id={ERROR_ID} className="text-destructive text-sm" role="alert">
            {shown}
          </p>
        )}
      </div>
    </form>
  );
}
