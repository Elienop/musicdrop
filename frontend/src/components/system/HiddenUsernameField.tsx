/** What a password manager files this server's single password under. There is
 * no account to name — one password protects the whole instance — so the entry
 * is named after the app rather than after a user who does not exist. */
const ACCOUNT_NAME = "musicdrop";

/**
 * The username half of a password form, for the password managers that expect
 * one.
 *
 * MusicDrop has a single password and no accounts, so its forms carry password
 * fields only — and Chrome logs "Password forms should have (optionally hidden)
 * username fields for accessibility" for each of them, while managers file
 * entries they then struggle to offer back on the right screen.
 *
 * Off-screen rather than `display: none` / `hidden`: an omitted field is the
 * thing managers skip, and the point is for the form to HAVE one. It is
 * `readOnly` so nothing here can be edited into a different value, `tabIndex`
 * -1 and `aria-hidden` so it is not a stop in the keyboard order and not a
 * field a screen-reader user is asked to fill in — the operator is told about
 * one password, and this is not a second thing to type.
 */
export function HiddenUsernameField() {
  return (
    <input
      type="text"
      name="username"
      autoComplete="username"
      value={ACCOUNT_NAME}
      readOnly
      tabIndex={-1}
      aria-hidden="true"
      className="sr-only"
    />
  );
}
