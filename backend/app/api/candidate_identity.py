"""Shared identity selection for the up-front duplicate checks.

Both the bank endpoint (``GET /bank/{item_id}/duplicates``) and the live
import endpoint (``GET /import/{job_id}/albums/{index}/duplicates``) detect
against the SELECTED candidate option's own metadata so the up-front check
equals what the apply does (the apply pins this option's release_id and beets
runs find_duplicates on ITS albumartist/album). The album_after fallback is
ONLY for the top option (idx == 0, where album_after IS the selection) and
legacy rows with no options: a NON-TOP option must never borrow the top
match's artist/album, or the heads-up would query the wrong release (top
identity + a different option's release_id). A non-top option that lacks a
field leaves it None — its release_id carries the identity.
"""

from app.models.import_models import AlbumChange, CandidateOption


def selected_option_identity(
    options: list[CandidateOption],
    candidate_index: int,
    album_after: AlbumChange,
) -> tuple[str | None, str | None, int | None, str | None]:
    """Resolve (albumartist, album, year, mb_albumid) for the selected option.

    ``candidate_index`` clamps to 0 (the top option) when out of range, even
    though the API layer already enforces ge=0. See the module docstring for
    the non-top / top / legacy fallback rule.
    """
    idx = candidate_index if 0 <= candidate_index < len(options) else 0
    opt = options[idx] if options else None
    if opt is not None and idx != 0:
        return opt.album_artist, opt.album, opt.year, opt.release_id
    return (
        opt.album_artist if opt and opt.album_artist is not None else album_after.artist,
        opt.album if opt and opt.album is not None else album_after.album,
        opt.year if opt and opt.year is not None else album_after.year,
        opt.release_id if opt else None,
    )
