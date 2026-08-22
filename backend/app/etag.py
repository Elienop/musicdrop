"""A small shared helper for deriving an opaque ETag from a file's ``stat``.

This lives at the ``app`` root (not inside ``app/beets/`` or ``app/artwork/``)
so both packages can import it without crossing their layering boundaries:
``app/artwork/`` must never import from ``app/beets/`` (hard project boundary),
so the shared code must sit *outside* both.
"""

import os


def stat_etag(path: str | os.PathLike[str]) -> str | None:
    """Return an opaque ETag ``"{mtime_ns}-{size}"`` from a file's ``stat``.

    This is a ``stat`` only — it never reads the file. Returns ``None`` when the
    file cannot be stat'd (``OSError``), e.g. because it vanished after the caller
    checked for it. This helper is caller-agnostic: it documents neither *which*
    race a caller is guarding against nor *how* a caller falls back on failure.
    Keep that caller-specific knowledge at the call site, not here."""

    try:
        st = os.stat(path)
    except OSError:
        return None
    return f'"{st.st_mtime_ns}-{st.st_size}"'
