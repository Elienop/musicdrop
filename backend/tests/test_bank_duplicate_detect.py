"""Bank duplicate detection — the adapter + the lazy endpoint.

The adapter (`find_import_duplicates`) calls beets' OWN `Album.duplicates_query`
with the configured `import.duplicate_keys.album` (default `albumartist album`)
on the matched-release metadata, so its prediction equals what the apply does.
The endpoint surfaces it for a banked candidate row.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from beets.library import Item, Library
from fastapi.testclient import TestClient

from app.bank import store
from app.beets.duplicates import find_import_duplicates
from app.beets.library import LibraryHandle
from app.config import settings
from app.models.import_models import (
    AlbumChange,
    Candidate,
    CandidateOption,
    ParkedAlbum,
    Recommendation,
)


def _add_album(lib: Library, *, artist: str, album: str, mb: str | None, n: int) -> int:
    items = [
        Item(album=album, albumartist=artist, title=f"t{i}", track=i + 1, mb_albumid=mb or "")
        for i in range(n)
    ]
    al = lib.add_album(items)  # adds items too
    al.store()
    return int(al.id)


# ----- adapter (uses the beets_library fixture directly) -----


def test_detects_existing_album_by_default_keys(beets_library: LibraryHandle) -> None:
    lib = beets_library.lib
    aid = _add_album(lib, artist="2Pac", album="Me Against the World", mb="mb-x", n=15)
    found = find_import_duplicates(
        lib, albumartist="2Pac", album="Me Against the World", year=1995, mb_albumid="mb-x"
    )
    assert [e.album_id for e in found] == [aid]
    assert found[0].album_artist == "2Pac"


def test_no_collision_returns_empty(beets_library: LibraryHandle) -> None:
    lib = beets_library.lib
    _add_album(lib, artist="2Pac", album="Me Against the World", mb="mb-x", n=15)
    assert (
        find_import_duplicates(lib, albumartist="Nas", album="Illmatic", year=1994, mb_albumid=None)
        == []
    )


def test_no_albumartist_skips_like_beets(beets_library: LibraryHandle) -> None:
    # beets find_duplicates returns [] when artist is None (tasks.py:398-400).
    assert (
        find_import_duplicates(
            beets_library.lib, albumartist=None, album="x", year=None, mb_albumid=None
        )
        == []
    )


# ----- exclude_under: a re-import of the same folder is not its own collision -----


def _add_album_with_paths(lib: Library, *, artist: str, album: str, base: Path, n: int) -> int:
    """Add an album whose item files live under ``base`` (real, absolute paths).

    ``find_import_duplicates``'s same-folder exclusion reads ``item.path``; the
    bare ``_add_album`` helper leaves it empty, so the exclude branch never runs.
    ``base`` is deliberately outside the library's music dir so beets stores the
    paths absolute (no relative re-expansion to reason about).
    """
    base.mkdir(parents=True, exist_ok=True)
    items: list[Item] = []
    for i in range(n):
        it = Item(album=album, albumartist=artist, title=f"t{i}", mb_albumid="")
        it.path = os.fsencode(str(base / f"t{i}.mp3"))
        items.append(it)
    al = lib.add_album(items)
    al.store()
    return int(al.id)


def test_exclude_under_drops_a_reimport_of_the_same_folder(
    beets_library: LibraryHandle, tmp_path: Path
) -> None:
    lib = beets_library.lib
    base = tmp_path / "src" / "Album"
    _add_album_with_paths(lib, artist="Air", album="Moon Safari", base=base, n=10)
    # exclude_under == the album's own folder -> every file is under it -> dropped
    # (covers the `continue`; a folder is never a duplicate of itself).
    assert (
        find_import_duplicates(lib, albumartist="Air", album="Moon Safari", exclude_under=str(base))
        == []
    )


def test_exclude_under_unrelated_dir_keeps_the_album(
    beets_library: LibraryHandle, tmp_path: Path
) -> None:
    lib = beets_library.lib
    base = tmp_path / "src" / "Album"
    aid = _add_album_with_paths(lib, artist="Air", album="Moon Safari", base=base, n=10)
    found = find_import_duplicates(
        lib,
        albumartist="Air",
        album="Moon Safari",
        exclude_under=str(tmp_path / "somewhere" / "else"),
    )
    assert [e.album_id for e in found] == [aid]


def test_exclude_under_sibling_string_prefix_keeps_the_album(
    beets_library: LibraryHandle, tmp_path: Path
) -> None:
    # Files under ".../Album"; exclude_under ".../Alb" shares a STRING prefix but
    # is not a path-boundary ancestor. The `+ os.sep` boundary must keep the
    # album (drop `+ os.sep` and this album would be wrongly excluded).
    lib = beets_library.lib
    parent = tmp_path / "src"
    base = parent / "Album"
    aid = _add_album_with_paths(lib, artist="Air", album="Moon Safari", base=base, n=10)
    found = find_import_duplicates(
        lib,
        albumartist="Air",
        album="Moon Safari",
        exclude_under=str(parent / "Alb"),
    )
    assert [e.album_id for e in found] == [aid]


# ----- endpoint (uses the lifespan-less client wired to a real library) -----


@pytest.fixture()
def bank_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # Overrides beets_library's settings.beets_dir so get_bank_dir() points at a
    # dedicated bank tree (mirrors test_bank_api.py); the library stays on
    # app.state.beets_library, independent of this setting.
    monkeypatch.setattr(settings, "beets_dir", str(tmp_path / "beets"))
    return tmp_path / "beets" / "bank"


def _candidate(*, artist: str, album: str, release_ids: list[str | None]) -> Candidate:
    change = AlbumChange(
        artist=artist, album=album, year=1995, label=None, country=None, media=None
    )
    return Candidate(
        confidence=80.0,
        recommendation=Recommendation.medium,
        data_source="MusicBrainz",
        data_url=None,
        cover_after_url=None,
        has_current_art=False,
        changed_fields=[],
        album_before=change,
        album_after=change,
        tracks=[],
        missing=[],
        unmatched=[],
        options=[
            CandidateOption(
                index=i,
                confidence=80.0,
                data_source="MusicBrainz",
                disambiguation=None,
                release_id=rid,
            )
            for i, rid in enumerate(release_ids)
        ],
    )


@pytest.fixture()
def parked_row_in_library(bank_dir: Path, beets_library: LibraryHandle) -> str:
    """A needs_review row whose matched release collides with a library album."""
    _add_album(beets_library.lib, artist="2Pac", album="Me Against the World", mb="mb-1", n=15)
    parked = ParkedAlbum(
        album_index=0,
        folder="/inbox/2Pac",
        candidate=_candidate(
            artist="2Pac", album="Me Against the World", release_ids=["mb-1", "mb-2"]
        ),
    )
    item = store.create_item(
        bank_dir,
        folder="/inbox/2Pac",
        source="sweep",
        reason="needs_review",
        fingerprint="f" * 64,
        parked=parked,
    )
    return item.id


@pytest.fixture()
def no_match_row(bank_dir: Path, beets_library: LibraryHandle) -> str:
    item = store.create_item(
        bank_dir,
        folder="/inbox/Unknown",
        source="sweep",
        reason="no_match",
        fingerprint="f" * 64,
    )
    return item.id


def test_duplicates_endpoint_flags_a_collision(
    client: TestClient, bank_dir: Path, parked_row_in_library: str
) -> None:
    r = client.get(f"/api/bank/{parked_row_in_library}/duplicates", params={"candidate_index": 0})
    assert r.status_code == 200
    existing = r.json()["existing"]
    assert len(existing) == 1
    assert existing[0]["album_artist"] == "2Pac"


def test_duplicates_endpoint_404_when_row_gone(client: TestClient, bank_dir: Path) -> None:
    assert client.get("/api/bank/" + "0" * 32 + "/duplicates").status_code == 404


def test_duplicates_endpoint_empty_for_no_match_row(
    client: TestClient, bank_dir: Path, no_match_row: str
) -> None:
    # A row with no parked candidate has nothing to check.
    assert client.get(f"/api/bank/{no_match_row}/duplicates").json()["existing"] == []


# ----- detection follows the SELECTED option's metadata, not the top match -----


def _option(
    *,
    index: int,
    release_id: str | None,
    album_artist: str | None = None,
    album: str | None = None,
    year: int | None = None,
) -> CandidateOption:
    return CandidateOption(
        index=index,
        confidence=80.0,
        data_source="MusicBrainz",
        disambiguation=None,
        release_id=release_id,
        album_artist=album_artist,
        album=album,
        year=year,
    )


def _candidate_with_options(
    *, artist: str, album: str, options: list[CandidateOption]
) -> Candidate:
    change = AlbumChange(
        artist=artist, album=album, year=1995, label=None, country=None, media=None
    )
    return Candidate(
        confidence=80.0,
        recommendation=Recommendation.medium,
        data_source="MusicBrainz",
        data_url=None,
        cover_after_url=None,
        has_current_art=False,
        changed_fields=[],
        album_before=change,
        album_after=change,
        tracks=[],
        missing=[],
        unmatched=[],
        options=options,
    )


def _bank_row(bank_dir: Path, candidate: Candidate) -> str:
    parked = ParkedAlbum(album_index=0, folder="/inbox/Foo", candidate=candidate)
    item = store.create_item(
        bank_dir,
        folder="/inbox/Foo",
        source="sweep",
        reason="needs_review",
        fingerprint="f" * 64,
        parked=parked,
    )
    return item.id


def test_duplicates_endpoint_follows_the_selected_option_metadata(
    client: TestClient, bank_dir: Path, beets_library: LibraryHandle
) -> None:
    # Two distinct in-library albums by the SAME artist — only the selected
    # option's album title should pick out which one collides.
    lib = beets_library.lib
    foo_id = _add_album(lib, artist="The Band", album="Foo", mb="mb-foo", n=10)
    deluxe_id = _add_album(lib, artist="The Band", album="Foo (Deluxe)", mb="mb-dlx", n=14)
    # Top match (album_after) is "Foo"; the two options carry distinct titles.
    candidate = _candidate_with_options(
        artist="The Band",
        album="Foo",
        options=[
            _option(index=0, release_id="mb-foo", album_artist="The Band", album="Foo", year=1995),
            _option(
                index=1,
                release_id="mb-dlx",
                album_artist="The Band",
                album="Foo (Deluxe)",
                year=1995,
            ),
        ],
    )
    row_id = _bank_row(bank_dir, candidate)

    top = client.get(f"/api/bank/{row_id}/duplicates", params={"candidate_index": 0})
    assert top.status_code == 200
    assert [e["album_id"] for e in top.json()["existing"]] == [foo_id]

    deluxe = client.get(f"/api/bank/{row_id}/duplicates", params={"candidate_index": 1})
    assert deluxe.status_code == 200
    assert [e["album_id"] for e in deluxe.json()["existing"]] == [deluxe_id]


def test_duplicates_endpoint_top_option_falls_back_to_album_after(
    client: TestClient, bank_dir: Path, beets_library: LibraryHandle
) -> None:
    # A legacy banked row whose options predate the per-option metadata fields
    # (album_artist/album/year all None): the TOP option (idx 0) still detects
    # via album_after, since album_after IS the top match.
    lib = beets_library.lib
    foo_id = _add_album(lib, artist="The Band", album="Foo", mb="mb-foo", n=10)
    _add_album(lib, artist="The Band", album="Foo (Deluxe)", mb="mb-dlx", n=14)
    candidate = _candidate_with_options(
        artist="The Band",
        album="Foo",
        options=[
            _option(index=0, release_id="mb-foo"),
            _option(index=1, release_id="mb-dlx"),
        ],
    )
    row_id = _bank_row(bank_dir, candidate)

    r = client.get(f"/api/bank/{row_id}/duplicates", params={"candidate_index": 0})
    assert r.status_code == 200
    assert [e["album_id"] for e in r.json()["existing"]] == [foo_id]


def test_duplicates_endpoint_non_top_bare_option_uses_only_its_own_identity(
    client: TestClient, bank_dir: Path, beets_library: LibraryHandle
) -> None:
    # A non-top option that lacks its own album_artist/album must NOT borrow the
    # top match's identity: mixing the top match's artist/album with a different
    # option's release_id would show a heads-up for the WRONG release. With no
    # album_artist of its own, the check keys on the release_id alone (beets'
    # own no-artist guard then yields nothing rather than a wrong-release hit).
    lib = beets_library.lib
    _add_album(lib, artist="The Band", album="Foo", mb="mb-foo", n=10)
    _add_album(lib, artist="The Band", album="Foo (Deluxe)", mb="mb-dlx", n=14)
    candidate = _candidate_with_options(
        artist="The Band",
        album="Foo",
        options=[
            _option(index=0, release_id="mb-foo"),
            _option(index=1, release_id="mb-dlx"),
        ],
    )
    row_id = _bank_row(bank_dir, candidate)

    r = client.get(f"/api/bank/{row_id}/duplicates", params={"candidate_index": 1})
    assert r.status_code == 200
    # No album_artist on the option -> no borrowed "The Band"/"Foo" identity ->
    # the top match's "Foo" album is NOT wrongly reported for the deluxe option.
    assert r.json()["existing"] == []


def test_bank_duplicates_non_top_option_spies_only_its_own_fields(
    client: TestClient,
    bank_dir: Path,
    beets_library: LibraryHandle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Spy the query args directly (parity with the import endpoint test): a
    # non-top bare option is queried with albumartist=None and its OWN
    # release_id, never the top match's artist.
    import app.api.bank as bank_mod

    calls: list[dict[str, object]] = []

    def spy(lib: object, **kwargs: object) -> list[object]:
        calls.append(kwargs)
        return []

    monkeypatch.setattr(bank_mod, "find_import_duplicates", spy)
    candidate = _candidate_with_options(
        artist="The Band",
        album="Foo",
        options=[
            _option(index=0, release_id="mb-foo"),
            _option(index=1, release_id="mb-dlx"),
        ],
    )
    row_id = _bank_row(bank_dir, candidate)

    client.get(f"/api/bank/{row_id}/duplicates", params={"candidate_index": 1})
    non_top = calls[-1]
    assert non_top["albumartist"] is None
    assert non_top["album"] is None
    assert non_top["year"] is None
    assert non_top["mb_albumid"] == "mb-dlx"

    client.get(f"/api/bank/{row_id}/duplicates", params={"candidate_index": 0})
    top = calls[-1]
    assert top["albumartist"] == "The Band"
    assert top["album"] == "Foo"
    assert top["mb_albumid"] == "mb-foo"


# ----- the consolidated ExistingAlbum mapper (tracks + release identity) -----


def test_existing_album_carries_tracks_and_release(beets_library: LibraryHandle) -> None:
    from app.beets.existing_album import to_existing_album

    _add_album(beets_library.lib, artist="2Pac", album="All Eyez on Me", mb="mb-9", n=3)
    album = next(iter(beets_library.lib.albums()))
    existing = to_existing_album(beets_library.lib, album)
    assert existing.track_count == 3
    assert len(existing.tracks) == 3
    # disc-then-number order, fields mapped
    assert [t.track for t in existing.tracks] == sorted(
        t.track for t in existing.tracks if t.track is not None
    )
    assert existing.release is not None  # identity now present on the up-front check


def test_upfront_duplicates_response_includes_tracks(
    client: TestClient, bank_dir: Path, parked_row_in_library: str
) -> None:
    r = client.get(f"/api/bank/{parked_row_in_library}/duplicates", params={"candidate_index": 0})
    assert r.status_code == 200
    existing = r.json()["existing"]
    assert len(existing) == 1
    assert isinstance(existing[0]["tracks"], list)
    assert len(existing[0]["tracks"]) == 15
    first = existing[0]["tracks"][0]
    assert set(first) == {"track", "disc", "title", "format", "bitrate_kbps"}
