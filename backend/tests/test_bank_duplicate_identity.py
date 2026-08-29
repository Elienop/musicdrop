"""The banked-duplicate identity check — ``duplicate_albums_still_present``.

The adapter both duplicate-enforcement callers lean on: the bank apply runner
asks it "does the copy the user chose to keep still exist?" before refusing an
import, and the import session asks it "is this stored id still the album we
are allowed to Trash?" before moving one.

Why an id alone is not an answer: beets album ids are SQLite rowids on an
``id INTEGER PRIMARY KEY`` table, so a deleted album's id is handed to the next
insert. Every test here is a field of that hazard.
"""

from __future__ import annotations

from pathlib import Path

from beets.library import Item, Library

from app.beets.library import duplicate_albums_still_present
from app.models.album import ReleaseIdentity
from app.models.import_models import ExistingAlbum
from tests.conftest import build_library


def _library(tmp_path: Path) -> Library:
    return build_library(str(tmp_path / "library.db"), str(tmp_path / "music"))


def _add(
    lib: Library,
    *,
    artist: str = "Radiohead",
    album: str = "OK Computer",
    mb: str = "",
    data_source: str | None = None,
) -> int:
    beets_album = lib.add_album(
        [Item(albumartist=artist, album=album, title="Airbag", track=1, mb_albumid=mb)]
    )
    if data_source is not None:
        beets_album.data_source = data_source
    beets_album.store()
    return int(beets_album.id or 0)


def _stored(
    album_id: int,
    *,
    artist: str | None = "Radiohead",
    album: str | None = "OK Computer",
    release_url: str | None = None,
) -> ExistingAlbum:
    return ExistingAlbum(
        album_id=album_id,
        album_artist=artist,
        album=album,
        year=None,
        track_count=1,
        format=None,
        bitrate_kbps=None,
        folder="/lib/x",
        release=(
            None
            if release_url is None
            else ReleaseIdentity(
                data_source="MusicBrainz",
                label=None,
                country=None,
                media=None,
                disambiguation=None,
                release_url=release_url,
            )
        ),
    )


def test_present_and_matching_survives(tmp_path: Path) -> None:
    lib = _library(tmp_path)
    album_id = _add(lib)
    assert duplicate_albums_still_present(lib, [_stored(album_id)]) == [album_id]


def test_a_missing_id_does_not_survive(tmp_path: Path) -> None:
    lib = _library(tmp_path)
    album_id = _add(lib)
    assert duplicate_albums_still_present(lib, [_stored(album_id + 999)]) == []


def test_case_only_difference_still_matches(tmp_path: Path) -> None:
    # DELIBERATELY laxer than beets' byte-exact duplicate query: a case-only
    # re-tag is the same album, and reading it as a stranger would silently
    # un-enforce the user's decision. Kills "drop the casefold".
    lib = _library(tmp_path)
    album_id = _add(lib, artist="radiohead", album="ok computer")
    assert duplicate_albums_still_present(lib, [_stored(album_id)]) == [album_id]


def test_surrounding_whitespace_still_matches(tmp_path: Path) -> None:
    lib = _library(tmp_path)
    album_id = _add(lib, artist="  Radiohead ", album="OK Computer  ")
    assert duplicate_albums_still_present(lib, [_stored(album_id)]) == [album_id]


def test_a_different_album_title_does_not_survive(tmp_path: Path) -> None:
    # The id-reuse case with the artist still matching: only the ALBUM check
    # separates these two, so this is what kills dropping it.
    lib = _library(tmp_path)
    album_id = _add(lib, artist="Radiohead", album="Kid A")
    assert duplicate_albums_still_present(lib, [_stored(album_id)]) == []


def test_a_different_album_artist_does_not_survive(tmp_path: Path) -> None:
    # Mirror image: same title, different artist — kills dropping the ARTIST
    # check. Both halves need their own fixture or one can be deleted freely.
    lib = _library(tmp_path)
    album_id = _add(lib, artist="Thom Yorke", album="OK Computer")
    assert duplicate_albums_still_present(lib, [_stored(album_id)]) == []


def test_a_different_release_does_not_survive(tmp_path: Path) -> None:
    # Names match but the copy was re-tagged to another release: the stored
    # release URL is the third discriminator, and it disagrees.
    lib = _library(tmp_path)
    album_id = _add(lib, mb="mb-other", data_source="MusicBrainz")
    stored = _stored(album_id, release_url="https://musicbrainz.org/release/mb-banked")
    assert duplicate_albums_still_present(lib, [stored]) == []


def test_the_same_release_survives(tmp_path: Path) -> None:
    # The control for the test above: identical URLs must NOT be read as a
    # mismatch, or the release check would reject every match it inspects.
    lib = _library(tmp_path)
    album_id = _add(lib, mb="mb-banked", data_source="MusicBrainz")
    stored = _stored(album_id, release_url="https://musicbrainz.org/release/mb-banked")
    assert duplicate_albums_still_present(lib, [stored]) == [album_id]


def test_a_stored_release_survives_an_untagged_library_copy(tmp_path: Path) -> None:
    # Only compared when BOTH sides carry one: an untagged library copy has no
    # URL to disagree with, so the name match stands rather than failing shut.
    lib = _library(tmp_path)
    album_id = _add(lib)  # no mb_albumid, no data_source -> no release URL
    stored = _stored(album_id, release_url="https://musicbrainz.org/release/mb-banked")
    assert duplicate_albums_still_present(lib, [stored]) == [album_id]


def test_a_composed_stored_name_matches_a_decomposed_library_name(tmp_path: Path) -> None:
    # The two Unicode spellings of "Beyoncé" render identically and name the
    # same album; a re-tag can rewrite one as the other. Kills dropping the NFC
    # normalize from _fold — casefold alone leaves these unequal.
    # Written as ESCAPES so an editor that normalized this file cannot quietly
    # turn it into a same-bytes comparison that proves nothing.
    decomposed = "Beyonce\u0301"  # e + COMBINING ACUTE ACCENT
    composed = "Beyonc\u00e9"  # LATIN SMALL LETTER E WITH ACUTE
    assert decomposed != composed
    lib = _library(tmp_path)
    album_id = _add(lib, artist=decomposed, album="Lemonade")
    stored = _stored(album_id, artist=composed, album="Lemonade")
    assert duplicate_albums_still_present(lib, [stored]) == [album_id]


def test_a_blank_named_entry_needs_the_url_to_disagree_to_be_rejected(tmp_path: Path) -> None:
    # Blank names on BOTH sides compare equal (None == None), so the URL is the
    # entire identity here. A reused rowid now holding a different untagged
    # release must not match.
    lib = _library(tmp_path)
    album_id = _add(lib, artist="", album="", mb="mb-other", data_source="MusicBrainz")
    stored = _stored(
        album_id, artist=None, album=None, release_url="https://musicbrainz.org/release/mb-banked"
    )
    assert duplicate_albums_still_present(lib, [stored]) == []


def test_a_blank_named_entry_fails_shut_when_the_live_copy_has_no_url(tmp_path: Path) -> None:
    # THE hole this pair exists to close: with no names to compare and no live
    # URL, nothing about the album was ever verified — an untagged stranger on a
    # reused rowid would match, and a `replace` would trash it. Absent must fail
    # SHUT, not fall through to the name match the way a NAMED entry does.
    lib = _library(tmp_path)
    album_id = _add(lib, artist="", album="")  # no mb_albumid, no data_source
    stored = _stored(
        album_id, artist=None, album=None, release_url="https://musicbrainz.org/release/mb-banked"
    )
    assert duplicate_albums_still_present(lib, [stored]) == []


def test_a_blank_named_entry_survives_on_a_verified_url(tmp_path: Path) -> None:
    # The control for the two above: fail-shut must not become reject-always, or
    # the whole blank-names branch would be unreachable dead code.
    lib = _library(tmp_path)
    album_id = _add(lib, artist="", album="", mb="mb-banked", data_source="MusicBrainz")
    stored = _stored(
        album_id, artist=None, album=None, release_url="https://musicbrainz.org/release/mb-banked"
    )
    assert duplicate_albums_still_present(lib, [stored]) == [album_id]


def test_a_half_named_entry_with_a_url_fails_shut_without_a_live_url(tmp_path: Path) -> None:
    # HALF a name key is one discriminator, not two: "Greatest Hits" by nobody
    # matches every blank-artist album of that title a reused rowid could now
    # hold. The stored entry brought a release URL to that comparison, so the
    # URL has to be VERIFIED — carrying it and never checking it is the hole.
    # Under `replace`, matching here trashes an album nobody decided about.
    lib = _library(tmp_path)
    album_id = _add(lib, artist="", album="Greatest Hits")  # no mb_albumid
    stored = _stored(
        album_id,
        artist=None,
        album="Greatest Hits",
        release_url="https://musicbrainz.org/release/mb-banked",
    )
    assert duplicate_albums_still_present(lib, [stored]) == []


def test_a_half_named_entry_with_a_url_rejects_a_different_live_release(tmp_path: Path) -> None:
    # The disagreeing-URL case for the same shape: present but different is a
    # rejection whether the name key is whole or partial.
    lib = _library(tmp_path)
    album_id = _add(lib, artist="", album="Greatest Hits", mb="mb-other", data_source="MusicBrainz")
    stored = _stored(
        album_id,
        artist=None,
        album="Greatest Hits",
        release_url="https://musicbrainz.org/release/mb-banked",
    )
    assert duplicate_albums_still_present(lib, [stored]) == []


def test_a_half_named_entry_survives_on_a_verified_url(tmp_path: Path) -> None:
    # The control: fail-shut must not become reject-always, or a half-named
    # entry carrying a URL could never match anything again.
    lib = _library(tmp_path)
    album_id = _add(
        lib, artist="", album="Greatest Hits", mb="mb-banked", data_source="MusicBrainz"
    )
    stored = _stored(
        album_id,
        artist=None,
        album="Greatest Hits",
        release_url="https://musicbrainz.org/release/mb-banked",
    )
    assert duplicate_albums_still_present(lib, [stored]) == [album_id]


def test_a_half_named_entry_without_a_url_still_matches_on_the_name(tmp_path: Path) -> None:
    # The UNCHANGED arm, pinned so the fail-shut rule cannot quietly widen onto
    # it: with no URL ever stored, the single name is all the identity that was
    # recorded, so it is all that can be asked for. Tightening this instead
    # would un-enforce every decision banked from a half-tagged library copy.
    lib = _library(tmp_path)
    album_id = _add(lib, artist="", album="Greatest Hits")
    stored = _stored(album_id, artist=None, album="Greatest Hits")
    assert duplicate_albums_still_present(lib, [stored]) == [album_id]


def test_an_identityless_entry_never_survives(tmp_path: Path) -> None:
    # A stored entry with nothing to compare would match every sparsely tagged
    # album in the library — the id-reuse hazard with extra steps.
    lib = _library(tmp_path)
    album_id = _add(lib, artist="", album="")
    assert duplicate_albums_still_present(lib, [_stored(album_id, artist=None, album=None)]) == []


def test_partitions_a_mixed_list(tmp_path: Path) -> None:
    # The list form is what both callers pass: survivors come back, strangers
    # and ghosts are simply absent — never an error.
    lib = _library(tmp_path)
    good = _add(lib)
    stranger = _add(lib, artist="Someone", album="Else")
    assert duplicate_albums_still_present(
        lib, [_stored(good), _stored(stranger), _stored(good + 999)]
    ) == [good]
