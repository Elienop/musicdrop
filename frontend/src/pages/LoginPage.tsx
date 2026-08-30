import { type SubmitEvent, useEffect, useRef, useState } from "react";
import { Navigate, useLocation, useNavigate } from "react-router";

import { useAuthStatus, useLogin } from "@/api/auth";
import { useSignedOut } from "@/api/authStore";
import { ICON_WEIGHT, IconContext, Spinner, Warning } from "@/components/icons";
import { LogoWordmark } from "@/components/shell/Logo";
import { CopyableSnippet } from "@/components/system/CopyableSnippet";
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

/** Generating a hash is the operator's step, and README §Authentication is the
 * source for both forms of the command — the container one first, since that
 * is how MusicDrop is meant to run. */
const HASH_COMMANDS = `docker exec -it musicdrop python -m app.auth.hash_password
# or, from a checkout:  cd backend && uv run python -m app.auth.hash_password`;

/** Ties the rejection text to the field it is about, for `aria-describedby`.
 * Static, like the field's own id: this form is mounted once per page. */
const ERROR_ID = "login-password-error";

/** What RouteAnnouncer would have written if this page were inside the shell.
 * Same `"<Page> - MusicDrop"` shape, so the tab reads consistently either way. */
const PAGE_TITLE = "Sign in - MusicDrop";

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

  useEffect(() => {
    document.title = PAGE_TITLE;
  }, []);

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
                <h2>Sign in</h2>
              </CardTitle>
              <CardDescription>
                MusicDrop is protected by a single password.
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
 * has no password to check against, or there is a form to fill in. */
function LoginCardBody({
  status,
  destination,
  returning,
}: Readonly<{
  status: AuthStatusQuery;
  destination: string;
  returning: boolean;
}>) {
  if (status.isPending) {
    return <StatusProbePending />;
  }
  if (status.data?.password_set === false) {
    return <NoPasswordConfigured status={status} />;
  }
  return <SignInForm destination={destination} returning={returning} />;
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
 */
function sameOriginPath(raw: string): string | null {
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
  destination,
  returning,
}: Readonly<{ destination: string; returning: boolean }>) {
  const navigate = useNavigate();
  const login = useLogin();
  const [password, setPassword] = useState("");
  const passwordRef = useRef<HTMLInputElement>(null);

  function handleSubmit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    login.mutate(password, {
      onSuccess: () => {
        void navigate(destination, { replace: true });
      },
      onError: () => {
        // Submitting DISABLES the button, which drops focus to <body>; the
        // rejection then announces itself to a user whose focus is nowhere,
        // with the rejected password still typed out and needing a select-all.
        // Safe as a per-call callback, unlike the success side: a failure
        // leaves this form mounted exactly where it was.
        passwordRef.current?.focus();
        passwordRef.current?.select();
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
          // The primitive already styles `aria-invalid` (destructive border +
          // ring); without the attribute that styling could never fire, and
          // the rejection below was text sitting near the field rather than
          // text ABOUT it.
          aria-invalid={login.isError}
          aria-describedby={login.isError ? ERROR_ID : undefined}
          value={password}
          onChange={(e) => setPassword(e.target.value)}
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
      {login.isError && (
        <p id={ERROR_ID} className="text-destructive text-sm" role="alert">
          {login.error.message}
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

/** `password_set: false` covers both "MUSICDROP_PASSWORD_HASH is unset" and
 * "it is set to something unreadable" — the backend collapses them because the
 * operator's fix is identical, so the copy names that one fix rather than
 * guessing which happened. */
function NoPasswordConfigured({
  status,
}: Readonly<{ status: AuthStatusQuery }>) {
  // What the LAST re-check found, or null before the first one. The answer
  // that would change anything (a password now configured) swaps this whole
  // branch for the form, so anything recorded here is by definition "no
  // change" — see handleRecheck.
  const [recheck, setRecheck] = useState<null | "unchanged" | "unreachable">(
    null,
  );

  async function handleRecheck() {
    setRecheck(null);
    const result = await status.refetch();
    setRecheck(result.isError ? "unreachable" : "unchanged");
  }

  return (
    <div className="flex flex-col gap-4">
      {/* A warning, in the system's shape for one. It was muted prose wearing
          role="alert" — the tone said "aside", the role said "interrupt", and
          nothing on this server works until it is dealt with. */}
      <StatusBanner tone="warning" icon={Warning}>
        No password is configured on this server, so nobody can sign in yet.
        Generate a hash, set it as{" "}
        <code className="font-mono">MUSICDROP_PASSWORD_HASH</code>, and restart
        MusicDrop.
      </StatusBanner>
      <CopyableSnippet label="Generate a password hash" snippet={HASH_COMMANDS} />
      {recheck !== null && (
        // `<output>` IS role="status" (same polite live region), and it is the
        // native element for "the result of the thing you just did" — which is
        // exactly what this is. Safe as a flex item: `<output>` is display:
        // inline, but a flex container blockifies every child, so it lays out
        // as the block <p> did. StatusProbePending above is the same choice.
        <output className="text-muted-foreground text-sm">
          {recheck === "unreachable"
            ? "Couldn’t reach the server — it may still be restarting."
            : "Still no password configured."}
        </output>
      )}
      {/* The copy above asks the operator to restart MusicDrop, so the likeliest
          moment for this click is mid-restart — when a silent re-check that
          changes nothing is indistinguishable from a dead button. `refetch`
          leaves `isPending` false (v5: initial load only), so the spinner has
          to read `isFetching`. */}
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
    </div>
  );
}
