"""Bank duplicate detection — the adapter + the lazy endpoint.

The adapter (`find_import_duplicates`) calls beets' OWN `Album.duplicates_query`
with the configured `import.duplicate_keys.album` (default `albumartist album`)
on the matched-release metadata, so its prediction equals what the apply does.
The endpoint surfaces it for a banked candidate row.
"""

from __future__ import annotations

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
        Item(album=album, albumartist=artist, title=f"t{i}", mb_albumid=mb or "") for i in range(n)
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


def test_duplicates_endpoint_falls_back_to_album_after_for_legacy_rows(
    client: TestClient, bank_dir: Path, beets_library: LibraryHandle
) -> None:
    # A legacy banked row whose options predate the per-option metadata fields
    # (album_artist/album/year all None) must still detect via album_after.
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

    # Both indices fall back to album_after ("Foo") -> the plain "Foo" album.
    for idx in (0, 1):
        r = client.get(f"/api/bank/{row_id}/duplicates", params={"candidate_index": idx})
        assert r.status_code == 200
        assert [e["album_id"] for e in r.json()["existing"]] == [foo_id]
