import { type SubmitEvent, useEffect, useRef, useState } from "react";
import { Navigate, useLocation, useNavigate } from "react-router";

import { useAuthStatus, useLogin, useSetupPassword } from "@/api/auth";
import { useSignedOut } from "@/api/authStore";
import { ICON_WEIGHT, IconContext, Spinner, Warning } from "@/components/icons";
import { LogoWordmark } from "@/components/shell/Logo";
import { CopyableSnippet } from "@/components/system/CopyableSnippet";
import { HiddenUsernameField } from "@/components/system/HiddenUsernameField";
import { StatusBanner } from "@/components/system/StatusBanner";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";

/** Generating a hash by hand is the OVERRIDE path only — the env var beats the
 * stored password, so this is what the operator needs when they are fixing a
 * mangled `MUSICDROP_PASSWORD_HASH` rather than letting the app manage one.
 * README §Authentication is the source for both forms of the command; the
 * container one first, since that is how MusicDrop is meant to run. */
const HASH_COMMANDS = `docker exec -it musicdrop python -m app.auth.hash_password
# or, from a checkout:  cd backend && uv run python -m app.auth.hash_password`;

/** Ties the rejection text to the field it is about, for `aria-describedby`.
 * Static, like the field's own id: this form is mounted once per page. */
const ERROR_ID = "login-password-error";

/** The setup form's own twin of ERROR_ID. Separate constant, not a shared one:
 * the two forms are never mounted together, but an id reused across them would
 * make that a fact nothing enforces. */
const SETUP_ERROR_ID = "setup-password-error";

/** Said by the setup form when the two fields disagree — client-side, because a
 * mismatch costs a ~0.16 s scrypt derive to be told the same thing by the
 * server, and the server is not even asked (it takes one password field). */
const MISMATCH_MESSAGE = "The two passwords don’t match. Type them again.";

/** What a re-check says when the request itself failed. Shared by all three
 * "nobody can sign in" branches, which each offer the same button. */
const UNREACHABLE_RECHECK =
  "Couldn’t reach the server — it may still be restarting.";

/** Said in the sign-in form after a 409 on setup: a password arrived from
 * somewhere else while this page was open, so the card swapped itself for a
 * form the operator did not ask for. Without it the likeliest next move is to
 * type the password they just chose and be told it is incorrect. */
const SUPERSEDED_MESSAGE =
  "A password was set from elsewhere while this page was open. Sign in with that one.";

/** What RouteAnnouncer would have written if this page were inside the shell.
 * Same `"<Page> - MusicDrop"` shape, so the tab reads consistently either way.
 * Two titles, because this card is two screens: the setup branch's only action
 * is "Set password", and a tab reading "Sign in" over it asks the operator
 * whether they already have a password. */
const PAGE_TITLE = "Sign in - MusicDrop";
const SETUP_PAGE_TITLE = "Set a password - MusicDrop";

/** The query result this page branches on, threaded to the branch that needs
 * it. Taken from the hook rather than re-spelt as `UseQueryResult<AuthStatus>`
 * so it cannot drift from what `useAuthStatus` actually returns. */
type AuthStatusQuery = ReturnType<typeof useAuthStatus>;

/**
 * Sign-in, mounted OUTSIDE the app shell (a top-level sibling of the layout
 * route in main.tsx). Being outside is load-bearing twice over: none of the
 * shell's gated queries fire behind this page, and returning to the shell
 * afterwards mounts a fresh `useEventStream` rather than reviving one the
 * gate's 401 closed for good.
 *
 * The shell provides the app-wide icon weight and the document title; a route
 * outside it has to provide its own, or every glyph here silently renders a
 * heavier Phosphor default and the tab keeps announcing the page the user was
 * bounced off ("Artists - MusicDrop" over a sign-in form).
 */
export function LoginPage() {
  const location = useLocation();
  const destination = intendedDestination(location.state);
  const status = useAuthStatus();
  const signedOut = useSignedOut();
  // `signedOut` AND a remembered page means this browser was inside the shell a
  // moment ago — an expiry or a Sign out click, not a cold visit. A cold visit
  // to a gated URL is bounced by the STATUS probe, which never touches the
  // store, so the two cases are distinguishable without a second flag.
  const returning = signedOut && location.state != null;
  // The one branch whose action is not signing in. Derived from the same two
  // fields `LoginCardBody` branches on, so the header cannot describe a
  // different screen from the one below it.
  const firstRun =
    status.data?.password_set === false &&
    status.data.password_source === "none";

  useEffect(() => {
    document.title = firstRun ? SETUP_PAGE_TITLE : PAGE_TITLE;
  }, [firstRun]);

  // Only a POSITIVE answer redirects — not `useAuthGateState`, which reports a
  // failed probe as "authenticated" so an outage doesn't strand the shell. On
  // THIS page that rule would invert into bouncing away from the one form that
  // could still help, so an unreachable backend renders the form and lets the
  // sign-in attempt report the real failure.
  if (!signedOut && status.data?.authenticated === true) {
    return <Navigate to={destination} replace />;
  }

  return (
    <IconContext.Provider value={ICON_WEIGHT}>
      <div className="bg-background text-foreground flex min-h-svh items-center justify-center px-4 py-10">
        {/* The lockup sits ABOVE the card, not inside CardHeader: that header
            is a two-row grid for a title and its description, and a third
            child needed a gap override plus a padding nudge to separate the
            brand from the text. Out here the separation is one page-level gap,
            which is the caller's to own. */}
        <div className="flex w-full max-w-sm flex-col gap-6">
          <h1 aria-label="MusicDrop" className="flex justify-center">
            <LogoWordmark className="h-6 w-auto" />
          </h1>
          <Card>
            <CardHeader>
              <CardTitle>
                {/* CardTitle renders a div, so the card's name was not in the
                    document outline. The h2 goes inside rather than replacing
                    it: the primitive keeps its slot and its type, and
                    preflight resets the heading's own size/weight/margin so
                    nothing moves. */}
                <h2>{firstRun ? "Set a password" : "Sign in"}</h2>
              </CardTitle>
              {/* Branched here rather than answered with a second paragraph
                  under the body: the two muted lines were typographically
                  identical and read as one description with an odd gap in the
                  middle, the second half restating the first. The env and file
                  branches keep "Sign in" — their banners are about nobody
                  being able to. */}
              <CardDescription>
                {firstRun
                  ? "No password is set yet. Choose the one everyone will use to sign in to this server."
                  : "MusicDrop is protected by a single password."}
              </CardDescription>
            </CardHeader>
            <CardContent>
              <LoginCardBody
                status={status}
                destination={destination}
                returning={returning}
              />
            </CardContent>
          </Card>
        </div>
      </div>
    </IconContext.Provider>
  );
}

/** The three states this card can be in, as early returns rather than a
 * nested ternary (typescript:S3358) — the probe is still deciding, the server
 * has no usable password to check against, or there is a form to fill in. */
function LoginCardBody({
  status,
  destination,
  returning,
}: Readonly<{
  status: AuthStatusQuery;
  destination: string;
  returning: boolean;
}>) {
  // Held HERE, one level above both forms, because the fact it records is that
  // the setup form was replaced by the sign-in form: state inside either one
  // is unmounted by the very swap it exists to explain — and when the re-check
  // that would swap them fails, the form that stays is the setup one, so both
  // arms are handed the flag.
  const [superseded, setSuperseded] = useState(false);

  if (status.isPending) {
    return <StatusProbePending />;
  }
  if (status.data?.password_set === false) {
    return (
      <NoUsablePassword
        status={status}
        destination={destination}
        superseded={superseded}
        onSuperseded={() => setSuperseded(true)}
      />
    );
  }
  return (
    <SignInForm
      status={status}
      destination={destination}
      returning={returning}
      superseded={superseded}
    />
  );
}

/**
 * `password_set: false` means the EFFECTIVE hash does not parse, which is three
 * different situations with three different fixes — and the backend keeps them
 * apart in `password_source` precisely so this screen can stop guessing.
 *
 * * `"none"` — nothing is configured anywhere, so the fix is to set a password,
 *   and that is a form on this page rather than a shell command and a restart.
 * * `"env"` — `MUSICDROP_PASSWORD_HASH` is set to something unreadable. It wins
 *   over the stored password even like this (owner's ruling), so setup is NOT
 *   offered: a typo'd variable must not silently create a second credential
 *   that the corrected variable would then shadow.
 * * `"file"` — the stored hash is there and unreadable. Deleting it is the
 *   recovery, and also the forgotten-password route.
 */
function NoUsablePassword({
  status,
  destination,
  superseded,
  onSuperseded,
}: Readonly<{
  status: AuthStatusQuery;
  destination: string;
  superseded: boolean;
  onSuperseded: () => void;
}>) {
  const source = status.data?.password_source;
  if (source === "env") {
    return <UnreadableEnvHash status={status} />;
  }
  if (source === "file") {
    return <UnreadableStoredHash status={status} />;
  }
  return (
    <FirstRunSetup
      status={status}
      destination={destination}
      superseded={superseded}
      onSuperseded={onSuperseded}
    />
  );
}

/**
 * `RequireAuth` stashes the page that was asked for in router state so signing
 * in lands there rather than on the Overview. Read defensively: the state is
 * whatever the last navigation put there, including a hand-typed /login with
 * none at all.
 */
function intendedDestination(state: unknown): string {
  if (state === null || typeof state !== "object" || !("from" in state)) {
    return "/";
  }
  const from: unknown = (state as { from: unknown }).from;
  if (from === null || typeof from !== "object" || !("pathname" in from)) {
    return "/";
  }
  const { pathname, search } = from as { pathname: unknown; search?: unknown };
  if (typeof pathname !== "string") {
    return "/";
  }
  const raw = typeof search === "string" ? `${pathname}${search}` : pathname;
  // No special case for a `from` of /login: it cannot arise. RequireAuth is
  // the only writer of this state and it only ever renders under the shell
  // layout, which /login is a sibling of — and even a hand-crafted one could
  // not loop, because navigating to /login carries no state, so the next pass
  // finds none and falls back to "/" (verified by probe).
  return sameOriginPath(raw) ?? "/";
}

/**
 * The candidate destination as a path on THIS origin, or null if it points
 * anywhere else.
 *
 * `pathname.startsWith("/")` is not the same question and got the wrong
 * answer: `//evil.com` and `/\evil.com` both pass it and both resolve
 * cross-origin, and `search` was concatenated with no check at all, so a
 * `{pathname: "/", search: "/evil.com"}` composed into one. Resolving against
 * the page origin and comparing origins asks the actual question once — and
 * returning the URL's OWN pathname+search means we navigate to something the
 * parser normalised, never to the raw string we were handed.
 *
 * A merely MALFORMED `from` (no leading slash) resolves against the origin and
 * so stays in-app, landing on that path rather than on the Overview. That is
 * the honest reading of a nonsense destination, and it is in-app either way.
 *
 * TWO passes, because one pass was not CLOSED under its own output: dot
 * segments are stripped during resolution, so `/.//evil.com`, `/..//evil.com`
 * and `/x/..//evil.com` each resolved on this origin and then returned
 * `//evil.com` — the exact protocol-relative shape this guard exists to
 * reject, and a string the function itself rejects when handed it back. Latent
 * rather than live at the time of writing (the WHATWG parser normalises
 * `location.pathname` before router state can hold a dot segment, so no
 * reachable input was found), but the value we return is a destination, and a
 * destination this function would refuse must not be one it emits. The second
 * pass is enough to reach the fixed point: its input is already normalised, so
 * anything that survives it is unchanged by a third.
 *
 * Worth knowing for the severity if it ever does become reachable: the caller
 * uses `navigate(destination, { replace: true })`, and react-router 7.15.1
 * falls back to `window.location.assign` on a SecurityError for `push()` but
 * NOT for `replace()` — so dropping that one flag turns "throws" into a
 * working open redirect.
 */
function sameOriginPath(raw: string): string | null {
  const once = resolveOnThisOrigin(raw);
  if (once === null) {
    return null;
  }
  return resolveOnThisOrigin(once) === once ? once : null;
}

/** One resolution pass: `raw` read as a URL against the page origin, reduced
 * to the parser's OWN pathname+search — never the raw string we were handed. */
function resolveOnThisOrigin(raw: string): string | null {
  let url: URL;
  try {
    url = new URL(raw, window.location.origin);
  } catch {
    return null;
  }
  if (url.origin !== window.location.origin) {
    return null;
  }
  return `${url.pathname}${url.search}`;
}

/** The status probe decides between the form and the setup notice, so render
 * neither until it answers — a form that flips to "no password configured" a
 * beat later reads as a fault. */
function StatusProbePending() {
  return (
    <output className="text-muted-foreground flex items-center gap-3 text-sm">
      <Spinner className="size-5 animate-spin" aria-hidden="true" />
      <span className="sr-only">Checking this server…</span>
    </output>
  );
}

function SignInForm({
  status,
  destination,
  returning,
  superseded,
}: Readonly<{
  status: AuthStatusQuery;
  destination: string;
  returning: boolean;
  superseded: boolean;
}>) {
  const navigate = useNavigate();
  const login = useLogin();
  const [password, setPassword] = useState("");
  // The sentence currently on screen, and whether it is the one about the
  // password field. Held rather than read off `login.error`, which outlives
  // the typing that answers it — the same shape the setup form below and the
  // Account panel use, and for the same reason.
  const [shown, setShown] = useState<
    Readonly<{ message: string; aboutPassword: boolean }> | undefined
  >(undefined);
  const passwordRef = useRef<HTMLInputElement>(null);

  /** Drop the sentence if it is the one about this field: typing answers "that
   * password was refused". A 429, a 503 or an unreachable server is not
   * answered by typing, so it stands until the next submit decides again. */
  function clearRefusal() {
    setShown((error) => (error?.aboutPassword === true ? undefined : error));
  }

  function handleSubmit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    // Cleared before the new outcome is decided, so the answer to THIS submit
    // is the only sentence on screen and the field is marked only while the
    // sentence is about it.
    setShown(undefined);
    login.mutate(password, {
      onSuccess: () => {
        void navigate(destination, { replace: true });
      },
      onError: (error) => {
        // A 401 is the answer about the value in this field. The route also
        // answers 401 when NOTHING on the server can authenticate — no hash,
        // or one that does not parse (backend/app/api/auth.py::_refusal_detail)
        // — which reaches this form only when the server's state changed while
        // the page was open, since the card branches on the status probe at
        // load. Rather than reading the server's prose to tell the two apart,
        // the re-check below re-asks: if nothing can authenticate, the answer
        // swaps this form for the branch that names the fix. The other
        // answers (429, 503, and a request with no answer at all) are about
        // the request or the server, so they are said in the alert and point
        // at no field.
        const aboutPassword = error.status === 401;
        setShown({ message: error.message, aboutPassword });
        if (aboutPassword) {
          // The setup form's move on a 409, for the same reason: the screen
          // that fits the server's current state is the one to be on.
          void status.refetch();
        }
        // Submitting DISABLES the button, which drops focus to <body>; the
        // rejection then announces itself to a user whose focus is nowhere.
        // Safe as a per-call callback, unlike the success side: a failure
        // leaves this form mounted exactly where it was. The select-all is for
        // the refused password only — it is the one answer saying the typed
        // value is the problem, and selecting after a 429 would put a retry's
        // first keystroke through the password the user still needs.
        passwordRef.current?.focus();
        if (aboutPassword) {
          passwordRef.current?.select();
        }
      },
    });
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-4">
      {returning && (
        // Deliberately one line for BOTH ways of getting here: after an expiry
        // it is the explanation the page otherwise never gave, and after a
        // Sign out click it is the only confirmation that the click worked.
        // Telling them apart would need the store to carry a reason, and the
        // wording that fits both is the wording neither user has to decode.
        <p className="text-muted-foreground text-sm">
          You’ve been signed out. Sign in again to continue.
        </p>
      )}
      {superseded && (
        // `<output>` IS role="status", and it is the native element for "the
        // result of the thing you just did" — here, a Set password click the
        // server answered 409. It is mounted with its text (the swap
        // re-renders the whole branch), which some screen readers do not
        // announce; the autoFocus below still lands the user in the field the
        // sentence is about. Same choice as StatusProbePending and
        // RecheckStatus below.
        <output className="text-muted-foreground text-sm">
          {SUPERSEDED_MESSAGE}
        </output>
      )}
      <HiddenUsernameField />
      <div className="flex flex-col gap-1">
        <label htmlFor="login-password" className="text-sm font-medium">
          Password
        </label>
        <Input
          id="login-password"
          type="password"
          autoComplete="current-password"
          autoFocus
          required
          ref={passwordRef}
          // Marked while the sentence on screen is about THIS field — a
          // refused password. The primitive already styles `aria-invalid`
          // (destructive border + ring). It used to read `login.isError`,
          // which marked the field for a busy derive and a missing signing
          // secret too: `aria-describedby` claims the sentence is ABOUT this
          // value, so a screen-reader user was told their password was the
          // problem when the server was.
          aria-invalid={shown?.aboutPassword === true}
          aria-describedby={
            shown?.aboutPassword === true ? ERROR_ID : undefined
          }
          value={password}
          onChange={(e) => {
            clearRefusal();
            setPassword(e.target.value);
          }}
        />
      </div>
      {/* The server authors every rejection here — wrong password, no hash
          configured, an unreadable one, a sign-in already in flight, a missing
          signing secret — and each names its own cause. Rendered verbatim so
          this page cannot describe a failure differently from the API. The one
          message written HERE is for the case the server never answered at
          all (see useLogin). The form stays enabled underneath: 429 in
          particular means "try that again", and disabling it would strand the
          one user who can. */}
      {shown !== undefined && (
        <p id={ERROR_ID} className="text-destructive text-sm" role="alert">
          {shown.message}
        </p>
      )}
      <Button type="submit" disabled={login.isPending}>
        {login.isPending && (
          <Spinner className="size-4 animate-spin" aria-hidden="true" />
        )}
        {login.isPending ? "Signing in…" : "Sign in"}
      </Button>
    </form>
  );
}


/**
 * First run: nothing is configured anywhere, so this screen sets the password
 * instead of explaining how to.
 *
 * Two fields and no reveal toggle, matching the CLI this replaces
 * (`app/auth/hash_password.py` prompts twice and refuses a mismatch): a typo in
 * a single masked field locks the operator out of their own server until they
 * delete a file and restart it, and the design system has no show/hide control
 * to offer instead.
 *
 * A 409 is not shown as text HERE. It means a password was configured while
 * this page was open — another browser, or the operator setting the env var —
 * so the response is to re-ask the server, let the answer swap this whole
 * branch for the sign-in form, and say what happened from over there (a
 * sentence in a form that is about to unmount is one the user watches
 * disappear).
 */
function FirstRunSetup({
  status,
  destination,
  superseded,
  onSuperseded,
}: Readonly<{
  status: AuthStatusQuery;
  destination: string;
  superseded: boolean;
  onSuperseded: () => void;
}>) {
  const navigate = useNavigate();
  const setup = useSetupPassword();
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  // The sentence currently on screen, and whether it is the one about the
  // confirm field. Held rather than read off `setup.error`, which outlives the
  // typing that answers it: a mismatch cleared by an edit uncovered the
  // server's previous rejection, which then reappeared under the fields.
  const [shown, setShown] = useState<
    Readonly<{ message: string; mismatch: boolean }> | undefined
  >(undefined);
  const passwordRef = useRef<HTMLInputElement>(null);
  const confirmRef = useRef<HTMLInputElement>(null);

  // The mismatch is checked here rather than as the fields are typed: a "they
  // don't match" that appears on the first keystroke of the second field is
  // telling the user they are wrong before they have finished being right.
  /** Drop the sentence if it is the mismatch: editing either half of the pair
   * answers the one sentence that is about the pair. A rejection the server
   * wrote is about the request, so typing does not answer it — it goes on the
   * next submit. */
  function clearMismatch() {
    setShown((error) => (error?.mismatch === true ? undefined : error));
  }

  function handleSubmit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    // Cleared before the new outcome is decided, so the answer to THIS submit
    // is the only sentence on screen and the only field marked is the one it
    // is about.
    setShown(undefined);
    if (password !== confirm) {
      setShown({ message: MISMATCH_MESSAGE, mismatch: true });
      confirmRef.current?.focus();
      confirmRef.current?.select();
      return;
    }
    setup.mutate(password, {
      onSuccess: () => {
        void navigate(destination, { replace: true });
      },
      onError: (error) => {
        if (error.status === 409) {
          // Re-ask, and hand the account of it to the form that replaces this
          // one. The refetch usually answers `password_set: true`, and
          // LoginCardBody swaps this branch for the sign-in form — which is
          // the thing the operator now needs, and the only screen that can act
          // on a password someone else just set. The 409's own sentence is not
          // rendered: `superseded` says the same fact in the words of what
          // happened here, in whichever form is on screen when it lands.
          onSuperseded();
          void status.refetch();
        } else {
          setShown({ message: error.message, mismatch: false });
        }
        // Submitting disabled the button, which drops focus to <body>. Every
        // answer that leaves this form mounted announces itself to a user
        // whose focus is nowhere, so put it back — including a 409 whose
        // re-check fails, which leaves this branch exactly where it was. No
        // `select()`: unlike a wrong password, none of these says the typed
        // value is the problem. Measured answers that land here: 422, 429 and
        // 503 from the route, and a request with no answer at all; the
        // middleware can also refuse a POST before the route sees it.
        passwordRef.current?.focus();
      },
    });
  }

  return (
    // The card header carries what this branch is (see LoginPage): a second
    // muted paragraph here said the same thing twice, in the same type, 12px
    // below the description.
    <form onSubmit={handleSubmit} className="flex flex-col gap-4">
      {superseded && (
        // Also HERE, not only in the sign-in form: the re-check behind a 409
        // can itself fail — a server mid-restart is the usual reason a 409
        // arrives — and then the cache still says `password_set: false`, this
        // branch stays mounted, and the click had changed nothing on screen.
        // The two are never mounted together: this form and the sign-in form
        // are the two arms of one branch.
        <output className="text-muted-foreground text-sm">
          {SUPERSEDED_MESSAGE}
        </output>
      )}
      <HiddenUsernameField />
      <div className="flex flex-col gap-1">
        <label htmlFor="setup-password" className="text-sm font-medium">
          Password
        </label>
        <Input
          id="setup-password"
          type="password"
          // `new-password`, not `current-password`: nothing is saved for this
          // server yet, so a manager should offer to generate and store one
          // rather than autofill an entry that cannot exist.
          autoComplete="new-password"
          autoFocus
          required
          ref={passwordRef}
          // No `aria-invalid` and no `aria-describedby`: nothing this form can
          // say is about the value in THIS field on its own. The mismatch is
          // answered by retyping the confirmation, and the server's rejections
          // are about the request or the server's state — marking this field
          // for them told a screen-reader user its contents were the problem.
          value={password}
          onChange={(e) => {
            // Clear on edit, from EITHER half: the mismatch was about the pair
            // as it stood at submit, and retyping the first field is the
            // likelier correction of the two.
            clearMismatch();
            setPassword(e.target.value);
          }}
        />
      </div>
      <div className="flex flex-col gap-1">
        <label htmlFor="setup-password-confirm" className="text-sm font-medium">
          Confirm password
        </label>
        <Input
          id="setup-password-confirm"
          type="password"
          autoComplete="new-password"
          required
          ref={confirmRef}
          // The MISMATCH only, and this field only: it is the one whose value
          // is wrong relative to the first, and the field "type them again"
          // sends the caret to. The server's rejections are about the request
          // or about the server's state, so they are said in the alert and
          // point at no field — the rule the Account panel's form follows too.
          aria-invalid={shown?.mismatch === true}
          aria-describedby={shown?.mismatch === true ? SETUP_ERROR_ID : undefined}
          value={confirm}
          onChange={(e) => {
            clearMismatch();
            setConfirm(e.target.value);
          }}
        />
      </div>
      {/* The server authors every rejection it sends — a blank password
          (422), a data directory it cannot write (503), a derive already
          running (429) — and each names its own cause, so they are rendered
          verbatim rather than re-worded here. The one sentence written on
          this side is the mismatch, which the server never sees. */}
      {shown !== undefined && (
        <p id={SETUP_ERROR_ID} className="text-destructive text-sm" role="alert">
          {shown.message}
        </p>
      )}
      <Button type="submit" disabled={setup.isPending}>
        {setup.isPending && (
          <Spinner className="size-4 animate-spin" aria-hidden="true" />
        )}
        {setup.isPending ? "Setting password…" : "Set password"}
      </Button>
    </form>
  );
}

/**
 * `MUSICDROP_PASSWORD_HASH` is set to something that does not parse.
 *
 * No setup form here, and that is the owner's ruling rather than an oversight:
 * the env var wins over the stored password whether or not its value can be
 * read, so offering setup on a typo'd variable would create a second credential
 * that the corrected variable then shadows — the operator would set a password,
 * fix the variable, and find the password gone.
 *
 * The compose sentence is the second half of the copy because it is the
 * measured way this state is reached: docker-compose interpolates a single `$`
 * that is followed by a letter, a digit or `_`, so a hash pasted straight from
 * the CLI usually arrives shorter than it left — 11 of 12 real hashes in the
 * probe behind this branch. The
 * backend says the same thing on its own 401
 * (`app/api/auth.py::_UNREADABLE_ENV_HASH_DETAIL`); this screen has to say it
 * itself because this branch makes no request that could carry it.
 */
function UnreadableEnvHash({ status }: Readonly<{ status: AuthStatusQuery }>) {
  return (
    <div className="flex flex-col gap-4">
      <StatusBanner tone="warning" icon={Warning}>
        <code className="font-mono">MUSICDROP_PASSWORD_HASH</code> is set on
        this server, but its value is not a password hash MusicDrop can read, so
        nobody can sign in. In docker-compose, every{" "}
        <code className="font-mono">$</code> in the hash must be doubled to{" "}
        <code className="font-mono">$$</code>.
      </StatusBanner>
      {/* The rest of the fix travels with the command it needs, in the
          snippet's own children slot (the shape SlskdPanel already uses):
          the banner said "fix or unset it" and the block said "generate",
          with nothing saying that fixing MEANS generating a fresh hash and
          pasting it back with doubled dollars.

          Both outcomes of unsetting, because this screen cannot tell them
          apart: status reports `password_source: "env"` and says nothing
          about a stored password shadowed behind the variable. Measured on a
          server that had both — after unsetting, the stored one took over and
          the sign-in screen asked for it. */}
      <CopyableSnippet label="Generate a password hash" snippet={HASH_COMMANDS}>
        <p className="text-muted-foreground text-sm">
          Generate a fresh hash with the command below and paste it into the
          variable, then restart MusicDrop. Or unset the variable and restart:
          if a password is stored on this server it applies again, otherwise
          this screen sets a new one.
        </p>
      </CopyableSnippet>
      <RecheckStatus
        status={status}
        unchanged="The hash in MUSICDROP_PASSWORD_HASH is still unreadable."
      />
    </div>
  );
}

/**
 * The stored hash exists and does not parse — a truncated write, a file edited
 * by hand, a half-copied backup.
 *
 * Deleting it is the recovery, and deliberately the only one offered: the
 * server refuses to overwrite a file it cannot read (`app/auth/source.py`),
 * because "unreadable" and "absent" would otherwise be the same state to this
 * screen and a corrupt file would silently re-open setup. This is also the
 * forgotten-password route, which is why the Settings panel's description says
 * the same thing.
 *
 * No path is printed. The file lives under whatever `MUSICDROP_BEETS_DIR` is
 * set to, which this screen is never told, and a plausible-looking wrong path
 * is worse than a named directory.
 */
function UnreadableStoredHash({
  status,
}: Readonly<{ status: AuthStatusQuery }>) {
  return (
    <div className="flex flex-col gap-4">
      <StatusBanner tone="warning" icon={Warning}>
        The password stored on this server is not readable, so nobody can sign
        in. Delete the <code className="font-mono">password-hash</code> file in
        MusicDrop’s beets directory and restart MusicDrop — this screen will then
        set a new password.
      </StatusBanner>
      <RecheckStatus
        status={status}
        unchanged="The stored password is still unreadable."
      />
    </div>
  );
}

/**
 * The "ask the server again" affordance, shared by all three branches above.
 *
 * Every one of them ends in "restart MusicDrop" or "someone else may have set
 * one", so the likeliest moment for this click is mid-restart — when a silent
 * re-check that changes nothing is indistinguishable from a dead button. The
 * answer that WOULD change something (a usable password) swaps the whole branch
 * for the sign-in form, so anything this records is by definition "no change",
 * and each branch passes its own wording for what did not change.
 *
 * `refetch` leaves `isPending` false (v5: initial load only), so the spinner has
 * to read `isFetching`.
 */
function RecheckStatus({
  status,
  unchanged,
}: Readonly<{ status: AuthStatusQuery; unchanged: string }>) {
  const [recheck, setRecheck] = useState<null | "unchanged" | "unreachable">(
    null,
  );

  async function handleRecheck() {
    setRecheck(null);
    const result = await status.refetch();
    setRecheck(result.isError ? "unreachable" : "unchanged");
  }

  return (
    <>
      {recheck !== null && (
        // `<output>` IS role="status" (same polite live region), and it is the
        // native element for "the result of the thing you just did" — which is
        // exactly what this is. Safe as a flex item: `<output>` is display:
        // inline, but a flex container blockifies every child, so it lays out
        // as the block <p> did. StatusProbePending above is the same choice.
        <output className="text-muted-foreground text-sm">
          {recheck === "unreachable" ? UNREACHABLE_RECHECK : unchanged}
        </output>
      )}
      <Button
        type="button"
        variant="outline"
        disabled={status.isFetching}
        onClick={() => void handleRecheck()}
      >
        {status.isFetching && (
          <Spinner className="size-4 animate-spin" aria-hidden="true" />
        )}
        {status.isFetching ? "Checking…" : "Check again"}
      </Button>
    </>
  );
}
