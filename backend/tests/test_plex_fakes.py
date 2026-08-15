"""Fidelity tests for tests/plex_fakes.py.

These pin the fake to plexapi 4.18.2's REAL semantics (read from the installed
playlist.py): items() is cached until reload(); removeItems/moveItem act on the
FIRST cached row per ratingKey; a DELETE of a gone row is NotFound; createPlaylist
rejects an empty list; smart playlists reject every mutator. If a future edit
"simplifies" the fake, these fail before a production bug can hide behind it.
"""

import pytest

from tests.plex_fakes import (
    FakeBadRequest,
    FakeNotFound,
    FakePlaylist,
    FakeServer,
    FakeTrack,
)


def _t(key: int) -> FakeTrack:
    return FakeTrack(key, [f"/m/{key}.flac"])


def test_items_is_cached_until_reload() -> None:
    a, b = _t(1), _t(2)
    pl = FakePlaylist("Mix", [a], 500)
    assert [t.ratingKey for t in pl.items()] == [1]
    pl.addItems([b])
    assert [t.ratingKey for t in pl.items()] == [1]  # stale: no reload yet
    assert pl.live_keys() == [1, 2]  # server truth
    pl.reload()
    assert [t.ratingKey for t in pl.items()] == [1, 2]


def test_remove_same_key_twice_off_stale_cache_raises_not_found() -> None:
    # plexapi: _getPlaylistItemID walks the CACHED items list and returns the first
    # ratingKey match -> both removes resolve to row #1; the second DELETE hits a
    # row the server no longer has -> NotFound. Exactly one occurrence goes.
    a = _t(1)
    pl = FakePlaylist("Mix", [a, _t(2), a], 500)  # 1, 2, 1
    pl.items()
    with pytest.raises(FakeNotFound):
        pl.removeItems([a, a])
    assert pl.live_keys() == [2, 1]


def test_remove_same_key_twice_with_reload_between_removes_both() -> None:
    a = _t(1)
    pl = FakePlaylist("Mix", [a, _t(2), a], 500)
    pl.reload()
    pl.removeItems([a])
    pl.reload()
    pl.removeItems([a])
    assert pl.live_keys() == [2]


def test_remove_key_not_in_cache_raises_not_found() -> None:
    pl = FakePlaylist("Mix", [_t(1)], 500)
    with pytest.raises(FakeNotFound):
        pl.removeItems([_t(9)])


def test_move_without_after_moves_to_front() -> None:
    a, b, c = _t(1), _t(2), _t(3)
    pl = FakePlaylist("Mix", [a, b, c], 500)
    pl.moveItem(b)
    assert pl.live_keys() == [2, 1, 3]


def test_move_after_places_immediately_after() -> None:
    a, b, c = _t(1), _t(2), _t(3)
    pl = FakePlaylist("Mix", [a, b, c], 500)
    pl.moveItem(a, after=c)
    assert pl.live_keys() == [2, 3, 1]


def test_move_added_item_before_reload_raises_not_found() -> None:
    a, b = _t(1), _t(2)
    pl = FakePlaylist("Mix", [a], 500)
    pl.items()
    pl.addItems([b])
    with pytest.raises(FakeNotFound):  # b is not in the stale cache
        pl.moveItem(b)


def test_smart_playlist_rejects_mutators() -> None:
    pl = FakePlaylist("Smart", [_t(1)], 500, smart=True)
    with pytest.raises(FakeBadRequest):
        pl.addItems([_t(2)])
    with pytest.raises(FakeBadRequest):
        pl.removeItems([_t(1)])
    with pytest.raises(FakeBadRequest):
        pl.moveItem(_t(1))


def test_server_rejects_empty_create_and_mints_distinct_keys() -> None:
    server = FakeServer([_t(1), _t(2)])
    with pytest.raises(FakeBadRequest):
        server.createPlaylist("Mix", items=[])
    p1 = server.createPlaylist("Mix", items=[_t(1)])
    p2 = server.createPlaylist("Mix", items=[_t(2)])
    assert (p1.ratingKey, p2.ratingKey) == (500, 501)
    assert server.playlists() == [p1, p2]
    p1.delete()
    assert server.playlists() == [p2]  # deleted playlists disappear from the listing


def test_switch_user_isolates_playlists_but_shares_library() -> None:
    server = FakeServer([_t(1)])
    user = server.switchUser("u1")
    assert user is server.switchUser("u1")  # stable per uid
    user.createPlaylist("Mix", items=[_t(1)])
    assert server.playlists() == []
    assert len(user.playlists()) == 1
    assert user.library.sections()[0] is server.library.sections()[0]
