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
          onClick={() => logout.mutate()}
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
