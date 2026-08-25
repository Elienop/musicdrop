from typing import Any

from app.beets.library import LibraryHandle, _require_id
from app.beets.playlist_match import build_match_index, match_entries


def _seed(
    lib: Any,
    *,
    title: str,
    artist: str,
    album: str = "Album",
    length: float = 200.0,
    filename: str | None = None,
) -> int:
    from beets.library import Item

    item = Item(
        title=title,
        artist=artist,
        albumartist=artist,
        album=album,
        length=length,
        path=(f"/lib/{artist}/{filename or title}.mp3").encode(),
    )
    item.add(lib)
    return _require_id(item.id)


def _entry(position: int = 0, **kwargs: Any) -> Any:
    from app.models.playlist_import import SourceEntry

    return SourceEntry(position=position, source=kwargs.get("path") or "src", **kwargs)


def test_t1_unique_filename_matches(beets_library: LibraryHandle) -> None:
    item_id = _seed(
        beets_library.lib,
        title="Around the World",
        artist="Daft Punk",
        filename="01 Around the World",
    )
    index = build_match_index(beets_library.lib)
    (result,) = match_entries(index, [_entry(path="D:\\Old\\01 Around the World.mp3")])
    assert result.status == "matched"
    assert result.item_id == item_id


def test_t2_normalized_artist_title_matches(beets_library: LibraryHandle) -> None:
    item_id = _seed(beets_library.lib, title="Song", artist="Blur")
    index = build_match_index(beets_library.lib)
    (result,) = match_entries(index, [_entry(artist="blur", title="Song (Remastered 2011)")])
    assert result.status == "matched"
    assert result.item_id == item_id


def test_duplicate_titles_need_duration_to_disambiguate(beets_library: LibraryHandle) -> None:
    short = _seed(
        beets_library.lib, title="Intro", artist="X", album="A", length=60.0, filename="a-intro"
    )
    _seed(beets_library.lib, title="Intro", artist="X", album="B", length=200.0, filename="b-intro")
    index = build_match_index(beets_library.lib)
    (with_duration,) = match_entries(
        index, [_entry(artist="X", title="Intro", duration_seconds=61.0)]
    )
    assert with_duration.status == "matched"
    assert with_duration.item_id == short
    (without,) = match_entries(index, [_entry(artist="X", title="Intro")])
    assert without.status == "ambiguous"
    assert len(without.suggestions) == 2


def test_unmatched_gets_title_suggestions(beets_library: LibraryHandle) -> None:
    _seed(beets_library.lib, title="Common Title", artist="Someone")
    index = build_match_index(beets_library.lib)
    (result,) = match_entries(index, [_entry(title="Common Title")])  # no artist
    assert result.status == "unmatched"
    assert [s.title for s in result.suggestions] == ["Common Title"]


def test_nothing_matches_nothing(beets_library: LibraryHandle) -> None:
    index = build_match_index(beets_library.lib)
    (result,) = match_entries(index, [_entry(title="Ghost Song", artist="Nobody")])
    assert result.status == "unmatched"
    assert result.suggestions == []


def test_interlude_title_normalizing_to_empty_still_matches(beets_library: LibraryHandle) -> None:
    # A title like "(Intro)" normalizes to "" (parentheticals are stripped), which
    # would guard it out of the index AND out of every lookup — leaving it unmatched
    # with zero suggestions even when the exact track exists. The raw-title fallback
    # rescues it.
    item_id = _seed(beets_library.lib, title="(Intro)", artist="X", filename="00 intro")
    index = build_match_index(beets_library.lib)
    (result,) = match_entries(index, [_entry(artist="X", title="(Intro)")])
    assert result.status == "matched"
    assert result.item_id == item_id
