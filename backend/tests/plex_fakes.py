"""One faithful test double for the plexapi playlist surface MusicDrop touches.

Semantics are copied from the INSTALLED plexapi 4.18.2 (playlist.py, base.py):

- ``items()`` is a cached property: it fetches once and stays STALE until
  ``reload()`` — no mutator refreshes it.
- ``removeItems``/``moveItem`` resolve the row to act on via the FIRST cached
  row whose ``ratingKey`` matches (``_getPlaylistItemID``); the argument's own
  row identity is ignored. A key absent from the cache -> NotFound. A DELETE of
  a row the server no longer has -> NotFound (HTTP 404).
- ``moveItem(item)`` with no ``after`` moves to the front; ``after=x`` places it
  right after the FIRST cached row of x (plexapi tests/test_playlist.py:70-80).
- Smart playlists raise BadRequest on all three mutators.
- ``createPlaylist`` with no items raises BadRequest; every created playlist
  gets a distinct ratingKey.

Method and attribute names are camelCase because they mirror plexapi's own
surface — production code calls them by those names.

Every fake in the test suite that stands in for a Plex playlist MUST come from
here — a fake whose ``removeItems`` clears everything lets a wrong diff pass.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class FakeNotFound(Exception):
    """Stands in for ``plexapi.exceptions.NotFound``."""


class FakeBadRequest(Exception):
    """Stands in for ``plexapi.exceptions.BadRequest``."""


class FakeTrack:
    def __init__(
        self,
        rating_key: int,
        locations: list[str],
        *,
        grandparentTitle: str = "",
        parentTitle: str = "",
        title: str = "",
        index: int | None = None,
    ) -> None:
        self.ratingKey = rating_key
        self.locations = locations
        self.grandparentTitle = grandparentTitle
        self.parentTitle = parentTitle
        self.title = title
        self.index = index


@dataclass
class _Row:
    """One server-side playlist row: a track plus its per-row playlistItemID."""

    track: FakeTrack
    row_id: int


class FakePlaylist:
    def __init__(
        self, title: str, items: list[FakeTrack], rating_key: int, *, smart: bool = False
    ) -> None:
        self.title = title
        self.ratingKey = rating_key
        self.summary = ""
        self.smart = smart
        self.deleted = False
        self.poster_uploads: list[str | None] = []
        self.calls: list[str] = []
        self._next_row_id = 1
        self._rows: list[_Row] = []
        for track in items:
            self._append(track)
        # What items() returns until reload(): None = not fetched yet.
        self._cache: list[_Row] | None = None

    # -- server truth (test-only helpers) ------------------------------------
    def live_keys(self) -> list[int]:
        return [row.track.ratingKey for row in self._rows]

    def _append(self, track: FakeTrack) -> None:
        self._rows.append(_Row(track, self._next_row_id))
        self._next_row_id += 1

    # -- plexapi surface ------------------------------------------------------
    def items(self) -> list[FakeTrack]:
        if self._cache is None:
            self._cache = list(self._rows)
        return [row.track for row in self._cache]

    def reload(self) -> FakePlaylist:
        self.calls.append("reload")
        self._cache = None
        return self

    def _first_cached_row_id(self, item: FakeTrack) -> int:
        self.items()  # plexapi's _getPlaylistItemID walks self.items() (fetching if cold)
        assert self._cache is not None
        for row in self._cache:
            if row.track.ratingKey == item.ratingKey:
                return row.row_id
        raise FakeNotFound(f"Item with ratingKey {item.ratingKey} not found in the playlist")

    def _guard_smart(self) -> None:
        if self.smart:
            raise FakeBadRequest("Cannot add or remove items from a smart playlist.")

    def addItems(self, items: list[FakeTrack] | FakeTrack) -> FakePlaylist:
        self._guard_smart()
        self.calls.append("addItems")
        tracks = items if isinstance(items, list) else [items]
        for track in tracks:
            self._append(track)
        return self

    def removeItems(self, items: list[FakeTrack] | FakeTrack) -> FakePlaylist:
        self._guard_smart()
        self.calls.append("removeItems")
        tracks = items if isinstance(items, list) else [items]
        for track in tracks:
            row_id = self._first_cached_row_id(track)
            live = [row for row in self._rows if row.row_id == row_id]
            if not live:
                raise FakeNotFound(f"playlist item {row_id} already gone (HTTP 404)")
            self._rows.remove(live[0])
        return self

    def moveItem(self, item: FakeTrack, after: FakeTrack | None = None) -> FakePlaylist:
        self._guard_smart()
        self.calls.append("moveItem")
        row_id = self._first_cached_row_id(item)
        after_id = self._first_cached_row_id(after) if after is not None else None
        moving = [row for row in self._rows if row.row_id == row_id]
        if not moving:
            raise FakeNotFound(f"playlist item {row_id} already gone (HTTP 404)")
        self._rows.remove(moving[0])
        if after_id is None:
            self._rows.insert(0, moving[0])
            return self
        anchor = [i for i, row in enumerate(self._rows) if row.row_id == after_id]
        if not anchor:
            raise FakeNotFound(f"playlist item {after_id} already gone (HTTP 404)")
        self._rows.insert(anchor[0] + 1, moving[0])
        return self

    def editTitle(self, title: str, locked: bool = True) -> FakePlaylist:
        self.calls.append("editTitle")
        self.title = title
        return self

    def editSummary(self, summary: str, locked: bool = True) -> FakePlaylist:
        self.calls.append("editSummary")
        self.summary = summary
        return self

    def uploadPoster(self, url: str | None = None, filepath: str | None = None) -> FakePlaylist:
        self.calls.append("uploadPoster")
        self.poster_uploads.append(filepath)
        return self

    def delete(self) -> None:
        self.calls.append("delete")
        self._rows = []
        self.deleted = True


class FakeSection:
    TYPE = "artist"

    def __init__(self, tracks: list[FakeTrack], *, title: str = "Music") -> None:
        self._tracks = tracks
        self.title = title

    def searchTracks(self) -> list[FakeTrack]:
        return self._tracks


@dataclass
class _Library:
    _sections: list[FakeSection] = field(default_factory=list)

    def sections(self) -> list[FakeSection]:
        return self._sections


class FakeServer:
    def __init__(
        self,
        tracks: list[FakeTrack],
        *,
        section_title: str = "Music",
        _library: _Library | None = None,
    ) -> None:
        self.library = _library or _Library([FakeSection(tracks, title=section_title)])
        self._tracks = tracks
        self.created: list[FakePlaylist] = []
        self._playlists: list[FakePlaylist] = []
        self._next_key = 500
        self.users: dict[str, FakeServer] = {}

    def playlists(self) -> list[FakePlaylist]:
        return [pl for pl in self._playlists if not pl.deleted]

    def createPlaylist(self, title: str, items: list[FakeTrack]) -> FakePlaylist:
        if not items:
            raise FakeBadRequest("Must include items to add when creating new playlist.")
        pl = FakePlaylist(title, items, self._next_key)
        self._next_key += 1  # every created playlist gets a distinct ratingKey
        self.created.append(pl)
        self._playlists.append(pl)
        return pl

    def switchUser(self, uid: str) -> FakeServer:
        if uid not in self.users:
            self.users[uid] = FakeServer(self._tracks, _library=self.library)
        return self.users[uid]
