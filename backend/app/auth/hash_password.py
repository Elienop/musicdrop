"""Generate a ``MUSICDROP_PASSWORD_HASH`` value.

    cd backend && uv run python -m app.auth.hash_password

Prompts twice (hidden), prints the ``scrypt$...`` string to stdout, and exits
non-zero on a mismatch, or on a password that is empty or only whitespace.

This is the OVERRIDE path, not the normal one: a server with no password shows a
setup form on its own sign-in screen, which writes the hash to
``<beets_dir>/password-hash`` (``app/auth/source.py``). The env var is for
regaining access to a deployment whose stored password was forgotten, and it
wins over the file while it is set.

Because the hash is going into a compose file more often than not, the guidance
on stderr also prints the ``$$``-doubled form. Compose treats a ``$`` as the
start of a variable reference only when a letter or an underscore follows it,
and the name it reads runs to the first character that is not a letter, digit or
underscore. So in a raw ``scrypt$...`` value the numeric fields survive
(``$131072$8$1`` arrives intact, as does base64 ``=`` padding), while a salt or
digest that starts with a letter loses its leading run — measured over 12
generated hashes with Compose 5.5.0, what arrives usually no longer parses, and
occasionally (both fields happening to start with a digit or a symbol) arrives
untouched and works. Doubling every ``$`` removes the question.

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
_BLANK_REFUSAL = "refusing to hash a password that is empty or only whitespace"


def main() -> int:
    password = getpass(_PROMPT)
    # ``.strip()``, so this refuses exactly what the two writing routes refuse
    # (``app/api/auth.py::_reject_a_blank_password``). A whitespace-only password
    # is the same mistake with a stray keystroke, and hashing one here produces a
    # working credential nobody could retype on purpose. ``app/models/auth.py``
    # and README both tell the operator the rules are the same; this is the line
    # that makes that true.
    if not password.strip():
        print(_BLANK_REFUSAL, file=sys.stderr)
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
