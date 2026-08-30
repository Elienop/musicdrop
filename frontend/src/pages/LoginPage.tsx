import { IconContext } from "@phosphor-icons/react";
import { type FormEvent, useState } from "react";
import { Navigate, useLocation, useNavigate } from "react-router";

import { useAuthStatus, useLogin } from "@/api/auth";
import { useSignedOut } from "@/api/authStore";
import { ICON_WEIGHT, Spinner } from "@/components/icons";
import { LogoWordmark } from "@/components/shell/Logo";
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

/**
 * Sign-in, mounted OUTSIDE the app shell (a top-level sibling of the layout
 * route in main.tsx). Being outside is load-bearing twice over: none of the
 * shell's gated queries fire behind this page, and returning to the shell
 * afterwards mounts a fresh `useEventStream` rather than reviving one the
 * gate's 401 closed for good.
 *
 * The shell provides the app-wide icon weight; a route outside it has to
 * provide its own or every glyph here silently renders a heavier Phosphor
 * default.
 */
export function LoginPage() {
  const location = useLocation();
  const destination = intendedDestination(location.state);
  const status = useAuthStatus();
  const signedOut = useSignedOut();

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
        <Card className="w-full max-w-sm">
          <CardHeader className="gap-3">
            <h1 aria-label="MusicDrop" className="flex justify-center pb-1">
              <LogoWordmark className="h-7 w-auto" />
            </h1>
            <CardTitle>Sign in</CardTitle>
            <CardDescription>
              MusicDrop is protected by a single password.
            </CardDescription>
          </CardHeader>
          <CardContent>
            {status.isPending ? (
              <StatusProbePending />
            ) : status.data?.password_set === false ? (
              <NoPasswordConfigured onRecheck={() => void status.refetch()} />
            ) : (
              <SignInForm destination={destination} />
            )}
          </CardContent>
        </Card>
      </div>
    </IconContext.Provider>
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
  // Must be an IN-APP path. `navigate()` follows an absolute URL, so anything
  // that isn't rooted at "/" would take the user off the app entirely — and
  // this reads a value that only has to LOOK like a Location.
  if (typeof pathname !== "string" || !pathname.startsWith("/")) {
    return "/";
  }
  // No special case for a `from` of /login: it cannot arise. RequireAuth is
  // the only writer of this state and it only ever renders under the shell
  // layout, which /login is a sibling of — and even a hand-crafted one could
  // not loop, because navigating to /login carries no state, so the next pass
  // finds none and falls back to "/" (verified by probe).
  return typeof search === "string" ? `${pathname}${search}` : pathname;
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

function SignInForm({ destination }: Readonly<{ destination: string }>) {
  const navigate = useNavigate();
  const login = useLogin();
  const [password, setPassword] = useState("");

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    login.mutate(password, {
      onSuccess: () => {
        void navigate(destination, { replace: true });
      },
    });
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-4">
      <div className="flex flex-col gap-1">
        <label htmlFor="login-password" className="text-sm font-medium">
          Password
        </label>
        <Input
          id="login-password"
          type="password"
          autoComplete="current-password"
          autoFocus
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
      </div>
      {/* The server authors every rejection here — wrong password, no hash
          configured, an unreadable one, a sign-in already in flight, a missing
          signing secret — and each names its own cause. Rendered verbatim so
          this page cannot describe a failure differently from the API. The
          form stays enabled underneath: 429 in particular means "try that
          again", and disabling it would strand the one user who can. */}
      {login.isError && (
        <p className="text-destructive text-sm" role="alert">
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
function NoPasswordConfigured({ onRecheck }: Readonly<{ onRecheck: () => void }>) {
  const [copied, setCopied] = useState(false);

  async function handleCopy() {
    try {
      await navigator.clipboard.writeText(HASH_COMMANDS);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard access can be denied (insecure context / permission); the
      // commands stay on screen to select manually, so a failed copy is a
      // no-op — the SlskdPanel webhook snippet does the same.
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <p className="text-muted-foreground text-sm" role="alert">
        No password is configured on this server, so nobody can sign in yet.
        Generate a hash, set it as <code>MUSICDROP_PASSWORD_HASH</code>, and
        restart MusicDrop.
      </p>
      <div className="flex flex-col gap-2">
        <div className="flex items-center justify-between gap-2">
          <p className="text-sm font-medium">Generate a password hash</p>
          <Button type="button" variant="outline" size="sm" onClick={() => void handleCopy()}>
            {copied ? "Copied" : "Copy"}
          </Button>
        </div>
        <pre className="bg-muted overflow-x-auto rounded-lg p-3 font-mono text-xs">
          {HASH_COMMANDS}
        </pre>
      </div>
      <Button type="button" variant="outline" onClick={onRecheck}>
        Check again
      </Button>
    </div>
  );
}
