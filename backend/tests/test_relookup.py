from typing import Any

import pytest
from beets import metadata_plugins
from beets.autotag import AlbumInfo, AlbumMatch, TrackInfo
from beets.autotag.distance import distance
from beets.autotag.match import Proposal, assign_items
from beets.autotag.match import Recommendation as BeetsRec
from beets.library import Item

import app.beets.relookup as rl
from app.models.import_models import ImportSearch


class _Task:
    """Minimal stand-in for ImportTask (relookup only reads ``.items``)."""

    def __init__(self, items: list[Item]) -> None:
        self.items = items
        self.candidates: list[Any] = []


def _items() -> list[Item]:
    return [Item(artist="2 Brothers", album="Dreams", title="Dreams", track=1, length=200.0)]


def _info(album_id: str, album: str) -> AlbumInfo:
    return AlbumInfo(
        tracks=[TrackInfo(title="Dreams", track_id="t1", index=1, length=200.0)],
        album=album,
        artist="2 Brothers on the 4th Floor",
        album_id=album_id,
        data_source="MusicBrainz",
        data_url=f"https://mb/{album_id}",
        year=1994,
        va=False,
    )


def _match(album_id: str, album: str, items: list[Item]) -> AlbumMatch:
    info = _info(album_id, album)
    pairs, extra_i, extra_t = assign_items(items, info.tracks)
    return AlbumMatch(distance(items, info, pairs), info, dict(pairs), extra_i, extra_t)


def test_relookup_release_id_uses_tag_album_search_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    items = _items()
    seen: dict[str, Any] = {}
    canned = _match("a1", "Dreams", items)

    def fake_tag_album(
        items_: Any, search_artist: Any = None, search_name: Any = None, search_ids: Any = None
    ) -> Any:
        seen["search_ids"] = search_ids
        return ("2 Brothers", "Dreams", Proposal([canned], BeetsRec.strong))

    monkeypatch.setattr(rl, "tag_album", fake_tag_album)
    cands, rec = rl.relookup(
        _Task(items), ImportSearch(release_id="https://musicbrainz.org/release/a1")
    )
    assert seen["search_ids"] == ["https://musicbrainz.org/release/a1"]
    assert cands == [canned]
    assert rec is BeetsRec.strong


def test_relookup_forced_non_va_calls_candidates_va_false(monkeypatch: pytest.MonkeyPatch) -> None:
    items = _items()
    seen: dict[str, Any] = {}

    def fake_candidates(items_: Any, artist: Any, album: Any, va_likely: Any) -> Any:
        seen["va"] = va_likely
        seen["artist"] = artist
        return [_info("a1", "Dreams")]  # AlbumInfo; relookup maps it via real _add_candidate

    # Patch the beets module directly (relookup imports the same module object).
    monkeypatch.setattr(metadata_plugins, "candidates", fake_candidates)
    cands, _rec = rl.relookup(
        _Task(items), ImportSearch(artist="2 Brothers", album="Dreams", force_non_va=True)
    )
    assert seen["va"] is False
    assert seen["artist"] == "2 Brothers"
    assert len(cands) == 1
    assert cands[0].info.album == "Dreams"


def test_relookup_default_name_search_uses_public_tag_album(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    items = _items()
    seen: dict[str, Any] = {}
    canned = _match("a1", "Dreams", items)

    def fake_tag_album(
        items_: Any, search_artist: Any = None, search_name: Any = None, search_ids: Any = None
    ) -> Any:
        seen["artist"] = search_artist
        seen["album"] = search_name
        seen["search_ids"] = search_ids
        return ("2 Brothers", "Dreams", Proposal([canned], BeetsRec.medium))

    monkeypatch.setattr(rl, "tag_album", fake_tag_album)
    cands, rec = rl.relookup(
        _Task(items), ImportSearch(artist="2 Brothers", album="Dreams", force_non_va=False)
    )
    assert seen == {"artist": "2 Brothers", "album": "Dreams", "search_ids": None}
    assert cands == [canned]
    assert rec is BeetsRec.medium


def test_relookup_empty_results(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_tag_album(*a: Any, **k: Any) -> Any:
        return ("x", "y", Proposal([], BeetsRec.none))

    monkeypatch.setattr(rl, "tag_album", fake_tag_album)
    cands, rec = rl.relookup(_Task(_items()), ImportSearch(release_id="bad-id"))
    assert cands == []
    assert rec is BeetsRec.none
