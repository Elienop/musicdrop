"""Pydantic contract for the Trash management view (list / restore / empty)."""

from typing import Literal

from pydantic import BaseModel

#: What Restore will DO to a given Trash row.
#:
#: * ``"move_back"`` — the folder carries an origin record naming a folder
#:   inside the current music library, so Restore moves it straight back there
#:   and re-imports it in place. Exact.
#: * ``"import"`` — no usable origin, so Restore re-imports the folder and beets
#:   files it under the CURRENT path templates, which is not necessarily where
#:   it was. ``restore_note`` says why.
#: * ``"refused"`` — Restore will do NOTHING, and neither will this row's own
#:   Empty. The entry is a symlink, and that is the whole predicate: a lexical
#:   ``os.path.islink`` on the entry itself (``trash_manage._is_symlinked_entry``),
#:   which the listing renders this value from and ``resolve_trash_child`` refuses
#:   the request with, so both per-row endpoints answer 404 before anything is
#:   resolved or touched. ONE function, two callers — the two are true together
#:   by construction rather than by agreement. (Written as "resolves outside the
#:   Trash dir", this claim was false for a link pointing at a SIBLING entry: the
#:   row read "refused" while the per-row Empty resolved through the link and
#:   removed the other row.) The UI must disable both controls on this value;
#:   leaving them live offers two buttons whose only possible outcome is an
#:   error. ``restore_note`` says where the album's files really are and what
#:   does remove the entry (only ``DELETE /api/trash/all``, and it removes the
#:   link alone).
#:
#: The third value says what the ROUTES will do, which is why it is allowed to
#: exist while ``track_count == 0`` still gets no value of its own. That count is
#: a GUESS — 0 there means "nothing here produced a readable media Item", not "no
#: music", and beets' own discovery takes every non-ignored file as a candidate,
#: so a 0-track folder can still import; encoding it as a contract value would
#: turn a UI hint into a promise the backend cannot keep. A symlinked entry is
#: the opposite kind of fact: the refusal comes from the guard every request to
#: those two routes passes through, so it is known before the click rather than
#: predicted. See ``trash_manage._audio_free_entries`` and
#: ``trash_manage._restore_fields``.
TrashRestoreMode = Literal["move_back", "import", "refused"]


class TrashedAlbum(BaseModel):
    """One album sitting in Trash (no beets DB row — read off disk).

    ``folder`` is the album's path RELATIVE to the Trash dir; it is the key the
    restore/empty endpoints take (resolved + traversal-checked server-side).

    ``restore_mode`` / ``restore_note`` / ``origin`` describe what Restore would
    do to THIS row, so the UI can say it before the user clicks rather than
    discovering it in the result. A row trashed before origins were recorded is
    ``"import"`` with a note stating that — visible and explained, never a
    Restore that silently lands somewhere else.

    Note for the UI: it does NOT disable Restore on ``track_count == 0`` and must
    not start — ``SettingsTrashPage.tsx`` deliberately shows a "may still work"
    hint instead, because 0 there means "no readable tags", not "no music". A
    recorded audio-free husk is exactly such a row AND is restorable exactly,
    which is the case this record was added for. ``restore_mode == "refused"``
    is the ONE signal that does disable a control, and it disables BOTH (Restore
    and this row's Empty), because both of those routes refuse the row outright.
    """

    folder: str
    album_artist: str | None
    album: str | None
    year: int | None
    track_count: int
    format: str | None
    restore_mode: TrashRestoreMode
    #: Why Restore is not exact, in a sentence fit to show the user. ``None``
    #: exactly when ``restore_mode`` is ``"move_back"`` — there is nothing to
    #: warn about then.
    restore_note: str | None
    #: The folder this was trashed from, display-safe and absolute; ``None``
    #: when nothing recorded it. Present even when ``restore_mode`` is
    #: ``"import"``, so a user can put an album back by hand.
    origin: str | None


class TrashListing(BaseModel):
    albums: list[TrashedAlbum]
    trash_path: str  # absolute Trash dir, shown so the user knows where it lives


class RestoreRequest(BaseModel):
    """Body of ``POST /api/trash/restore`` — the folder (relative to Trash)."""

    folder: str


class RestoreResult(BaseModel):
    """Outcome of a restore.

    ``already_in_library`` = a matching album is already present, so beets safely
    skipped; ``origin_occupied`` = something is at the path it came from again —
    a folder with anything in it, a file, or a symlink INCLUDING a broken one —
    so the move-back would have had to overwrite it, merge into it, or land
    beside it. In both cases the files are back in Trash, untouched.

    An EMPTY leftover folder at the origin is NOT occupied: it is replaced and
    the restore goes ahead. That is what a pruning beets or a half-finished sync
    leaves behind, and refusing it would strand exactly the rows the origin
    record exists for.
    """

    restored: bool
    reason: Literal["restored", "already_in_library", "could_not_restore", "origin_occupied"]
    album_id: int | None = None


class EmptyResult(BaseModel):
    removed: int  # folders permanently removed
