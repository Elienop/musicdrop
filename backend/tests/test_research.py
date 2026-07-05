from pathlib import Path
from typing import Any

import pytest
from beets.autotag import AlbumInfo, AlbumMatch, TrackInfo
from beets.autotag.distance import distance
from beets.autotag.match import Proposal, assign_items
from beets.autotag.match import Recommendation as BeetsRec
from beets.library import Item

import app.beets.research as res
from app.models.import_models import ImportSearch, Recommendation


def _items() -> list[Item]:
    return [Item(artist="2 Brothers", album="Dreams", title="Dreams", track=1, length=200.0)]


def _match(album_id: str, album: str, items: list[Item]) -> AlbumMatch:
    info = AlbumInfo(
        tracks=[TrackInfo(title="Dreams", track_id="t1", index=1, length=200.0)],
        album=album,
        artist="2 Brothers on the 4th Floor",
        album_id=album_id,
        data_source="MusicBrainz",
        data_url=f"https://mb/{album_id}",
        year=1994,
        va=False,
    )
    pairs, extra_i, extra_t = assign_items(items, info.tracks)
    return AlbumMatch(distance(items, info, pairs), info, dict(pairs), extra_i, extra_t)


def _searched(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    matches: list[AlbumMatch],
    rec: BeetsRec,
    items: list[Item],
) -> res.ResearchResult | None:
    (tmp_path / "01 dreams.mp3").write_bytes(b"not-audio")  # walker sees a file
    monkeypatch.setattr(res, "_read_items", lambda folder: items)
    monkeypatch.setattr(res, "relookup_items", lambda i, s: (matches, rec))
    return res.research_folder(str(tmp_path), ImportSearch(release_id="a1"))


def test_research_maps_the_full_candidate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    items = _items()
    result = _searched(
        monkeypatch, tmp_path, [_match("a1", "Dreams", items)], BeetsRec.strong, items
    )
    assert result is not None
    assert result.artist == "2 Brothers on the 4th Floor"
    assert result.album == "Dreams"
    assert result.recommendation is Recommendation.strong
    assert result.confidence > 0
    cand = result.candidate
    assert cand.album_after.album == "Dreams"
    assert cand.album_before.artist == "2 Brothers"  # get_most_common_tags over the items
    assert len(cand.options) == 1
    assert cand.options[0].release_id == "a1"
    assert cand.has_current_art is False  # in-memory items carry no art source


def test_research_none_when_lookup_is_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert _searched(monkeypatch, tmp_path, [], BeetsRec.none, _items()) is None


def test_research_none_when_folder_has_no_audio(tmp_path: Path) -> None:
    # Real walk, real Item.from_path: a non-media file must be skipped, not raise.
    (tmp_path / "cover.jpg").write_bytes(b"\xff\xd8\xff")
    (tmp_path / "notes.txt").write_text("hi")
    assert res.research_folder(str(tmp_path), ImportSearch(release_id="a1")) is None


def test_read_items_sorted_and_skips_non_media(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "b.mp3").write_bytes(b"x")
    (tmp_path / "a.mp3").write_bytes(b"x")
    (tmp_path / "cover.jpg").write_bytes(b"x")
    seen: list[str] = []

    def fake_from_path(path: bytes) -> Item:
        import os

        name = os.path.basename(os.fsdecode(path))
        if name.endswith(".jpg"):
            raise Exception("not media")
        seen.append(name)
        return Item(title=name)

    monkeypatch.setattr(res.Item, "from_path", staticmethod(fake_from_path))
    items = res._read_items(tmp_path)
    assert seen == ["a.mp3", "b.mp3"]  # deterministic path order
    assert len(items) == 2


def test_rescan_folder_no_audio_raises(tmp_path: Path) -> None:
    (tmp_path / "cover.jpg").write_bytes(b"\xff\xd8\xff")
    with pytest.raises(res.NoAudioFilesError):
        res.rescan_folder(str(tmp_path))


def test_rescan_folder_runs_the_default_lookup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    items = _items()
    canned = _match("a1", "Dreams", items)
    seen: dict[str, Any] = {}

    def fake_tag_album(items_: Any, *args: Any, **kwargs: Any) -> Any:
        seen["args"] = args
        seen["kwargs"] = kwargs
        return ("2 Brothers", "Dreams", Proposal([canned], BeetsRec.strong))

    monkeypatch.setattr(res, "_read_items", lambda folder: items)
    monkeypatch.setattr(res, "tag_album", fake_tag_album)
    outcome = res.rescan_folder(str(tmp_path))
    # Default first-scan lookup: NO search terms of any kind.
    assert seen["args"] == () and seen["kwargs"] == {}
    assert outcome.result is not None
    assert outcome.result.candidate.options[0].release_id == "a1"
    assert outcome.result.recommendation is Recommendation.strong
    assert outcome.cur_artist == "2 Brothers"
    assert outcome.cur_album == "Dreams"


def test_rescan_folder_no_candidates_keeps_cur_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(res, "_read_items", lambda folder: _items())
    monkeypatch.setattr(
        res, "tag_album", lambda items_: ("2 Brothers", "Dreams", Proposal([], BeetsRec.none))
    )
    outcome = res.rescan_folder(str(tmp_path))
    assert outcome.result is None
    assert outcome.cur_artist == "2 Brothers"
    assert outcome.cur_album == "Dreams"
    assert outcome.recommendation is Recommendation.none
