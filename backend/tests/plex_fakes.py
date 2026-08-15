"""One faithful test double for the plexapi playlist surface MusicDrop touches.

Semantics are copied from the INSTALLED plexapi 4.18.2 (playlist.py, base.py,
server.py, mixins/edit.py). Where the two could differ, the fake takes the
STRICTER branch: a fake that forgives more than Plex does is worse than no fake.

- ``items()`` is a cached property that returns the cached LIST OBJECT itself
  (``playlist.py:220-230`` returns ``self._items``). It stays STALE until
  ``reload()`` — no mutator refreshes it — and mutating the returned list
  corrupts the very list ``_getPlaylistItemID`` walks.
- Each fetched row is its OWN object carrying that row's ``playlistItemID``
  (``base.py:857-865``); two rows of one track are distinct objects that compare
  EQUAL, because plexapi compares by ``key`` and hashes by ``repr``
  (``base.py:639-645, 122-126``).
- ``removeItems``/``moveItem`` resolve the row to act on via the FIRST cached
  row whose ``ratingKey`` matches (``_getPlaylistItemID``, ``playlist.py:131-136``);
  the argument's own row identity is ignored. A key absent from the cache ->
  NotFound. A DELETE of a row the server no longer has -> NotFound (HTTP 404,
  ``server.py:749-756``).
- ``moveItem(item)`` with no ``after`` moves to the front; ``after=x`` places it
  right after the FIRST cached row of x. Moving a row after ITSELF (what a
  duplicate ratingKey resolves to) is refused: plexapi sends the request and PMS
  behaviour is undefined, so production must not rely on it.
- ``editTitle``/``editSummary`` only PUT (``mixins/edit.py:8-30`` ->
  ``base.py:716-726``): the in-memory attribute stays STALE until ``reload()``.
- Smart playlists raise BadRequest on all three item mutators; a DELETED
  playlist's key 404s, so every mutator raises NotFound.
- ``createPlaylist`` matches ``PlexServer.createPlaylist(title, section=None,
  items=None, ...)`` (``server.py:488``), so a second POSITIONAL argument binds
  to ``section`` and creates nothing, exactly as it would against a real server.
  ratingKeys come from ONE server-global space shared with every ``switchUser``
  server, because a playlist ratingKey is unique across accounts on a PMS.

Method and attribute names are camelCase because they mirror plexapi's own
surface — production code calls them by those names.

Every fake in the test suite that stands in for a Plex playlist MUST come from
here — a fake whose ``removeItems`` clears everything lets a wrong diff pass.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field


class FakeNotFound(Exception):
    """Stands in for ``plexapi.exceptions.NotFound``."""


class FakeBadRequest(Exception):
    """Stands in for ``plexapi.exceptions.BadRequest``."""


class _PlexIdentity:
    """Identity as plexapi does it: equal and hash-equal by ``key``.

    ``PlexPartialObject.__eq__`` compares ``self.key`` (``base.py:639-645``), so
    two objects standing for one track are interchangeable in ``in``, ``==``,
    ``set()`` and dict keys. Comparing by object identity instead would hide
    every reconcile bug that leans on those. (Real ``__hash__`` is
    ``hash(repr(self))`` — ratingKey + title, ``base.py:122-126`` — which we
    simplify to the key alone; the two differ only when two objects for one
    track disagree on title, which nothing here does.)
    """

    ratingKey: int

    @property
    def key(self) -> str:
        return f"/library/metadata/{self.ratingKey}"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, _PlexIdentity):
            return self.key == other.key
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.key)


class FakeTrack(_PlexIdentity):
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


class FakePlaylistItem(_PlexIdentity):
    """One row as ``items()`` hands it back: a track object carrying this row's
    ``playlistItemID``. Fetching snapshots the track's fields, as building the
    object from that response's XML does."""

    playlistItemID: int
    ratingKey: int
    locations: list[str]
    grandparentTitle: str
    parentTitle: str
    title: str
    index: int | None

    def __init__(self, track: FakeItem, playlist_item_id: int) -> None:
        self.playlistItemID = playlist_item_id
        self.ratingKey = track.ratingKey
        self.locations = track.locations
        self.grandparentTitle = track.grandparentTitle
        self.parentTitle = track.parentTitle
        self.title = track.title
        self.index = track.index


# What plexapi accepts wherever it wants "an item": a Track fetched from the
# library OR a row fetched from the playlist — both resolve by ratingKey.
FakeItem = FakeTrack | FakePlaylistItem


@dataclass
class _Row:
    """One server-side playlist row: a track plus its per-row playlistItemID."""

    track: FakeItem
    row_id: int


@dataclass
class _KeyAllocator:
    """The server's metadata id space, shared by every account's view of it."""

    next_key: int = 500

    def take(self) -> int:
        key = self.next_key
        self.next_key += 1
        return key


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
        self._live_title = title
        self._live_summary = ""
        self._next_row_id = 1
        self._rows: list[_Row] = []
        for track in items:
            self._append(track)
        # What items() returns until reload(): None = not fetched yet.
        self._cache: list[FakePlaylistItem] | None = None

    # -- server truth (test-only helpers) ------------------------------------
    def live_keys(self) -> list[int]:
        return [row.track.ratingKey for row in self._rows]

    def live_title(self) -> str:
        return self._live_title

    def live_summary(self) -> str:
        return self._live_summary

    def _append(self, track: FakeItem) -> None:
        self._rows.append(_Row(track, self._next_row_id))
        self._next_row_id += 1

    def _live_index(self, row_id: int) -> int | None:
        for position, row in enumerate(self._rows):
            if row.row_id == row_id:
                return position
        return None

    # -- plexapi surface ------------------------------------------------------
    def items(self) -> list[FakePlaylistItem]:
        if self._cache is None:
            self._cache = [FakePlaylistItem(row.track, row.row_id) for row in self._rows]
        return self._cache

    def reload(self) -> FakePlaylist:
        self.calls.append("reload")
        self._cache = None
        self.title = self._live_title
        self.summary = self._live_summary
        return self

    def _first_cached_row_id(self, item: FakeItem) -> int:
        # plexapi's _getPlaylistItemID walks self.items() (fetching if cold) and
        # takes the FIRST ratingKey match.
        for row in self.items():
            if row.ratingKey == item.ratingKey:
                return row.playlistItemID
        raise FakeNotFound(f"Item with ratingKey {item.ratingKey} not found in the playlist")

    def _guard_smart(self) -> None:
        if self.smart:
            raise FakeBadRequest("Cannot add or remove items from a smart playlist.")

    def _guard_deleted(self) -> None:
        # The smart check is client-side and fires first in plexapi; this one
        # stands in for the server's 404 on a key that no longer exists.
        if self.deleted:
            raise FakeNotFound(f"(404) not_found; playlist {self.ratingKey} no longer exists")

    @staticmethod
    def _as_list(
        items: Sequence[FakeItem] | FakeItem,
    ) -> list[FakeItem]:
        # plexapi coerces only what is neither list nor tuple (playlist.py:249-250);
        # a single item (the only other thing the hint admits) gets wrapped.
        if isinstance(items, (FakeTrack, FakePlaylistItem)):
            return [items]
        return list(items)

    def addItems(self, items: Sequence[FakeItem] | FakeItem) -> FakePlaylist:
        self._guard_smart()
        self._guard_deleted()
        self.calls.append("addItems")
        for track in self._as_list(items):
            self._append(track)
        return self

    def removeItems(self, items: Sequence[FakeItem] | FakeItem) -> FakePlaylist:
        self._guard_smart()
        self._guard_deleted()
        self.calls.append("removeItems")
        for track in self._as_list(items):
            row_id = self._first_cached_row_id(track)
            position = self._live_index(row_id)
            if position is None:
                raise FakeNotFound(f"(404) not_found; playlist item {row_id} is already gone")
            del self._rows[position]
        return self

    def moveItem(self, item: FakeItem, after: FakeItem | None = None) -> FakePlaylist:
        self._guard_smart()
        self._guard_deleted()
        self.calls.append("moveItem")
        # plexapi resolves BOTH ids before it sends anything (playlist.py:310-317).
        row_id = self._first_cached_row_id(item)
        after_id = self._first_cached_row_id(after) if after is not None else None
        if after_id == row_id:
            raise FakeBadRequest(
                "moveItem after itself: PMS behaviour is undefined; the fake refuses it"
            )
        position = self._live_index(row_id)
        if position is None:
            raise FakeNotFound(f"(404) not_found; playlist item {row_id} is already gone")
        if after_id is not None and self._live_index(after_id) is None:
            raise FakeNotFound(f"(404) not_found; playlist item {after_id} is already gone")
        row = self._rows.pop(position)
        if after_id is None:
            self._rows.insert(0, row)
            return self
        anchor = self._live_index(after_id)
        assert anchor is not None  # checked above, and the pop cannot have removed it
        self._rows.insert(anchor + 1, row)
        return self

    def editTitle(self, title: str, locked: bool = True) -> FakePlaylist:
        self._guard_deleted()
        self.calls.append("editTitle")
        self._live_title = title  # the attribute stays stale until reload()
        return self

    def editSummary(self, summary: str, locked: bool = True) -> FakePlaylist:
        self._guard_deleted()
        self.calls.append("editSummary")
        self._live_summary = summary  # the attribute stays stale until reload()
        return self

    def uploadPoster(self, url: str | None = None, filepath: str | None = None) -> FakePlaylist:
        self._guard_deleted()
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
        _keys: _KeyAllocator | None = None,
    ) -> None:
        self.library = _library or _Library([FakeSection(tracks, title=section_title)])
        self._tracks = tracks
        self._keys = _keys or _KeyAllocator()
        self.created: list[FakePlaylist] = []
        self._playlists: list[FakePlaylist] = []
        self.users: dict[str, FakeServer] = {}

    def playlists(self) -> list[FakePlaylist]:
        return [pl for pl in self._playlists if not pl.deleted]

    def createPlaylist(
        self,
        title: str,
        section: object = None,
        items: list[FakeTrack] | tuple[FakeTrack, ...] | None = None,
        **kwargs: object,
    ) -> FakePlaylist:
        if kwargs:
            raise NotImplementedError(f"the fake models regular playlists only: {sorted(kwargs)}")
        if not items:
            raise FakeBadRequest("Must include items to add when creating new playlist.")
        pl = FakePlaylist(title, list(items), self._keys.take())
        self.created.append(pl)
        self._playlists.append(pl)
        return pl

    def switchUser(self, uid: str) -> FakeServer:
        # Real plexapi returns a NEW PlexServer per call (server.py:269); the fake
        # keeps one per uid so a test can reach the playlists it created. The
        # library and the ratingKey space are the SERVER's, so both are shared.
        if uid not in self.users:
            self.users[uid] = FakeServer(self._tracks, _library=self.library, _keys=self._keys)
        return self.users[uid]
