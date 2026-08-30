import { toast } from "sonner";

import { useLogout } from "@/api/auth";
import { SignOut, Spinner } from "@/components/icons";
import { Button } from "@/components/ui/button";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";

/**
 * Sign out of this browser — quiet topbar chrome in the icon-action dialect
 * ActivityButton established: ghost, 56px hit area, 40px thin glyph, and a
 * tooltip whose text is also the accessible name.
 *
 * No navigation here. Clearing the cookie flips the auth store, and
 * `RequireAuth` is already watching it — the same single path that carries a
 * mid-session 401 to /login. A second `navigate` alongside it would be a
 * second way to leave the shell, free to drift from the first.
 *
 * The failure side IS this component's, though: a sign-out that fails leaves
 * the user in the shell with nothing changed, and the hook's message reaching
 * nobody made "nothing happened" the whole of the feedback. sonner is where
 * mutation failures go here (the activityToasts dialect), and the toast is a
 * per-call callback because — unlike the success path, which unmounts this
 * button — a failure leaves it mounted to receive one.
 */
export function SignOutButton() {
  const logout = useLogout();
  const label = logout.isPending ? "Signing out…" : "Sign out";

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          variant="ghost"
          size="icon-xl"
          aria-label={label}
          disabled={logout.isPending}
          onClick={() =>
            logout.mutate(undefined, {
              onError: (error) => toast.error(error.message),
            })
          }
          className="focus-ring text-muted-foreground hover:text-foreground"
        >
          {logout.isPending ? (
            <Spinner
              weight="thin"
              aria-hidden="true"
              className="size-10 animate-spin"
            />
          ) : (
            <SignOut weight="thin" aria-hidden="true" className="size-10" />
          )}
        </Button>
      </TooltipTrigger>
      <TooltipContent>{label}</TooltipContent>
    </Tooltip>
  );
}
