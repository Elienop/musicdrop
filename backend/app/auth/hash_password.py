"""Generate a ``MUSICDROP_PASSWORD_HASH`` value.

    cd backend && uv run python -m app.auth.hash_password

Prompts twice (hidden), prints the ``scrypt$...`` string to stdout, and exits
non-zero on a mismatch or an empty password.

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
    # stderr for the guidance, stdout for the value alone, so
    # `... > hash.txt` and `MUSICDROP_PASSWORD_HASH=$(...)` both do the
    # obvious thing.
    print("set this as MUSICDROP_PASSWORD_HASH and restart MusicDrop:", file=sys.stderr)
    print(hash_password(password))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
