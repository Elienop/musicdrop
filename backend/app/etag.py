"""Shared ETag helpers: an opaque ETag derived from a file's ``stat``.

Format contract — INTERNAL to this module:

* A tag is a QUOTED STRONG HTTP tag built as ``"{mtime_ns}-{size}"``.
* Size-variant markers (``-t`` for a thumbnail of the same file) are spliced
  INSIDE the closing quote by ``size_scoped_etag`` (``"123-456-t"``), so
  every tag stays one opaque quoted string.

Callers treat tags as opaque: they must never parse, re-quote, or splice
markers into a tag themselves — the marker splice below depends on the
quoted format above, so only this module may own it.

This lives at the ``app`` root (not inside ``app/beets/`` or ``app/artwork/``)
so both packages can import it without crossing their layering boundaries:
``app/artwork/`` must never import from ``app/beets/`` (hard project boundary),
so the shared code must sit *outside* both.
"""

import os
from typing import Literal, overload


def stat_etag(path: str | os.PathLike[str]) -> str | None:
    """Return an opaque ETag ``"{mtime_ns}-{size}"`` from a file's ``stat``.

    The tag is a QUOTED STRONG tag — the quoting is this module's internal
    format contract, never the caller's to manage.

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


@overload
def size_scoped_etag(validator: None, size: Literal["full", "thumb"]) -> None: ...


@overload
def size_scoped_etag(validator: str, size: Literal["full", "thumb"]) -> str: ...


def size_scoped_etag(validator: str | None, size: Literal["full", "thumb"]) -> str | None:
    """The revalidation tag for ONE size of one image served under one URL.

    ``size == "full"`` passes ``validator`` through UNCHANGED (``None`` stays
    ``None``). ``size == "thumb"`` with a set ``validator`` splices the ``"-t"``
    marker INSIDE the tag's closing quote (``"123-456-t"``), keeping one opaque
    QUOTED STRONG tag.

    Why a distinct tag: the thumb is a DIFFERENT entity than the full image
    (distinct bytes) under the same URL family, and a shared tag would let a
    cache/proxy serve a thumb response for a full request (or vice versa) on a
    matching If-None-Match. The splice is supported only HERE because it depends
    on this module's quoted-format contract — callers must not re-implement it.
    """
    if validator is None or size == "full":
        return validator
    return f'{validator[:-1]}-t"'
