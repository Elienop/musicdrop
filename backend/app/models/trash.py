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
#:
#: Deliberately two values and not three: "this row cannot be restored at all"
#: is not something the backend can know. ``track_count`` is the only signal it
#: has, and 0 there means "nothing here produced a readable media Item", not "no
#: music" — beets' own discovery takes every non-ignored file as a candidate, so
#: a 0-track folder can still import. Encoding that guess as a contract value
#: would turn a UI hint into a promise. See ``trash_manage._audio_free_entries``.
TrashRestoreMode = Literal["move_back", "import"]


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
    which is the case this record was added for.
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
    skipped; ``origin_occupied`` = the folder it came from exists again, so the
    move-back would have had to overwrite or land beside it. In both cases the
    files are back in Trash, untouched.
    """

    restored: bool
    reason: Literal["restored", "already_in_library", "could_not_restore", "origin_occupied"]
    album_id: int | None = None


class EmptyResult(BaseModel):
    removed: int  # folders permanently removed
