"""Generate a ``MUSICDROP_PASSWORD_HASH`` value.

    cd backend && uv run python -m app.auth.hash_password

Prompts twice (hidden), prints the ``scrypt$...`` string to stdout, and exits
non-zero on a mismatch or an empty password.

This is the OVERRIDE path, not the normal one: a server with no password shows a
setup form on its own sign-in screen, which writes the hash to
``<beets_dir>/password-hash`` (``app/auth/source.py``). The env var is for
regaining access to a deployment whose stored password was forgotten, and it
wins over the file while it is set.

Because the hash is going into a compose file more often than not, the guidance
on stderr also prints the ``$$``-doubled form. A ``scrypt$...`` value pasted
raw into ``docker-compose.yml`` is variable-interpolated: the ``$n``, ``$r``,
``$p`` and both base64 fields lose everything from each ``$`` to the next
delimiter, and the container comes up with a hash that cannot be parsed.

The password is never taken from ``argv`` and never echoed: an argument would
land in shell history and in every ``ps`` listing on the box, which is the same
class of mistake as putting the plaintext in an env var.

``getpass`` opens ``/dev/tty`` FIRST and only falls back to stdin when there is
no controlling terminal. Piping a password in is therefore NOT reliable: run
from an interactive shell, the prompt reads the keyboard and ignores the pipe.
Callers that must script it have to detach the child from the terminal
(``subprocess.run(..., start_new_session=True)``), which is what
``tests/test_auth_credentials.py`` does.
"""

from __future__ import annotations

import sys
from getpass import getpass

from app.auth.passwords import hash_password

_PROMPT = "Password: "
_CONFIRM_PROMPT = "Confirm password: "


def main() -> int:
    password = getpass(_PROMPT)
    if not password:
        print("refusing to hash an empty password", file=sys.stderr)
        return 2
    if getpass(_CONFIRM_PROMPT) != password:
        print("passwords do not match", file=sys.stderr)
        return 2
    stored = hash_password(password)
    # stderr for the guidance, stdout for the value alone, so
    # `... > hash.txt` and `MUSICDROP_PASSWORD_HASH=$(...)` both do the
    # obvious thing.
    print("set this as MUSICDROP_PASSWORD_HASH and restart MusicDrop:", file=sys.stderr)
    print(stored)
    # The compose form goes to stderr too, so stdout stays the single value
    # every existing caller reads. Salt and digest are standard base64
    # (app/auth/passwords.py), so the only `$` in the string are its five
    # separators and this replace is exactly the escaped spelling.
    print(
        "in docker-compose.yml every $ must be doubled to $$, so use this form:",
        file=sys.stderr,
    )
    print(stored.replace("$", "$$"), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
