"""Single-account cookie-session authentication (stdlib only).

Four modules, each with one job:

* ``passwords`` — the ``MUSICDROP_PASSWORD_HASH`` format and the scrypt verify;
* ``session`` — the HMAC-signed expiring token, the cookie, and the on-disk
  signing secret;
* ``gate`` — the ASGI middleware that refuses un-authenticated API traffic, and
  the ONE exempt-path predicate the OpenAPI overlay shares with it;
* ``hash_password`` — the ``python -m app.auth.hash_password`` generator CLI.

No new dependency: ``hashlib.scrypt``, ``hmac`` and ``secrets`` are stdlib.
"""
