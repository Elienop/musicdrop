"""Fidelity tests for tests/plex_fakes.py.

These pin the fake to plexapi 4.18.2's REAL semantics (read from the installed
playlist.py): items() is cached until reload() and hands back the cached LIST
OBJECT; removeItems/moveItem act on the FIRST cached row per ratingKey; a DELETE
of a gone row is NotFound; tracks compare by key; edits stay stale until reload();
createPlaylist rejects an empty list; smart playlists reject every mutator; a
listing row carries no `smart` until an attribute read pays for the refetch. If a
future edit "simplifies" the fake, these fail before a production bug can hide
behind it.

Two things here are NOT read off plexapi, because no client can read them: what
a server does with an add naming a track the playlist already holds, and the
exact URLs of the row-precise endpoints. The first is modelled BOTH ways behind
`duplicates=` and pinned in each mode below; the second is pinned against the
URLs plexapi's own removeItems/moveItem build, which is the closest thing to
evidence there is.
"""

from collections.abc import Callable

import pytest

from tests.plex_fakes import (
    DuplicateAdds,
    FakeBadRequest,
    FakeNotFound,
    FakePlaylist,
    FakeSection,
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


def test_a_listing_hands_back_freshly_fetched_attributes() -> None:
    # PlexServer.playlists() BUILDS its objects from a fresh fetch (server.py:772),
    # so a playlist read out of a new listing carries Plex's current title and
    # summary — unlike the caller's own object, which stays stale until reload().
    # Without this, a second sync sees a stale summary and re-stamps a marker that
    # is already there, and no test could ever catch the redundant PUT.
    server = FakeServer([_t(1)])
    pl = server.createPlaylist("Mix", items=[_t(1)])
    pl.editTitle("Renamed")
    pl.editSummary("MusicDrop-id:p1")
    assert (pl.title, pl.summary) == ("Mix", "")  # the caller's object is still stale
    fetched = server.playlists()[0]
    assert (fetched.title, fetched.summary) == ("Renamed", "MusicDrop-id:p1")
    assert "reload" not in fetched.calls  # a fetch is not a client-side reload


def test_a_listing_row_reads_smart_only_by_paying_for_the_refetch() -> None:
    # plexapi casts a `smart` attrib the row did not carry to None
    # (playlist.py:70), so on a PARTIAL listing object the __dict__ says "not
    # smart" for a playlist that IS smart, and only a plain attribute read — the
    # auto-reload at base.py:650-668, a full GET — answers truthfully. A fake
    # that set `smart` eagerly makes the two reads indistinguishable, and the
    # reconcile could be "optimised" to the __dict__ read with nothing failing.
    server = FakeServer([_t(1)])
    smart_pl = FakePlaylist("Smart", [_t(1)], 500, smart=True)
    plain_pl = FakePlaylist("Mix", [_t(1)], 501)
    server._playlists.extend([smart_pl, plain_pl])

    listed_smart, listed_plain = server.playlists()
    assert vars(listed_smart)["smart"] is None  # the cheap read would say "normal"...
    assert listed_smart.smart is True  # ...the attribute read refetches the truth
    assert listed_smart.reads == ["smart"]  # and it cost exactly one hidden GET
    assert vars(listed_smart)["smart"] is True  # now resolved, so a re-read is free
    assert listed_smart.smart is True
    assert listed_smart.reads == ["smart"]
    assert "reload" not in listed_smart.calls  # production called nothing

    # Control arm: a normal playlist answers False through the same refetch, so
    # the trap is about WHEN the value arrives, not about smart playlists only.
    assert vars(listed_plain)["smart"] is None
    assert listed_plain.smart is False
    assert listed_plain.reads == ["smart"]


def test_a_listing_makes_a_reloaded_playlist_partial_again() -> None:
    # Every listing builds fresh objects from the /playlists response, so a copy
    # that was reloaded (full) goes back to partial when it is re-listed — the
    # shape a second sync sees.
    server = FakeServer([_t(1)])
    pl = FakePlaylist("Smart", [_t(1)], 500, smart=True)
    server._playlists.append(pl)
    assert pl.reload().smart is True
    assert vars(pl)["smart"] is True
    assert vars(server.playlists()[0])["smart"] is None


def test_a_listing_drops_the_stale_item_cache() -> None:
    a, b = _t(1), _t(2)
    server = FakeServer([a, b])
    pl = server.createPlaylist("Mix", items=[a])
    assert [t.ratingKey for t in pl.items()] == [1]
    pl.addItems([b])
    assert [t.ratingKey for t in pl.items()] == [1]  # stale: no reload, no re-fetch
    assert [t.ratingKey for t in server.playlists()[0].items()] == [1, 2]


def test_per_call_item_keys_are_recorded_for_the_one_uri_hazard() -> None:
    # addItems/createPlaylist comma-join their items into ONE uri, and whether PMS
    # honours a repeated ratingKey there is unknowable from the client. The fake
    # cannot decide it either — it records what each call carried so a test can
    # assert production never sends a repeat inside one call.
    a, b = _t(1), _t(2)
    server = FakeServer([a, b])
    pl = server.createPlaylist("Mix", items=[a, b])
    assert server.create_calls == [[1, 2]]
    pl.addItems([b])
    pl.addItems([a, b])
    assert pl.add_calls == [[2], [1, 2]]


def test_honouring_server_keeps_every_row_it_was_sent() -> None:
    # The default mode, and the one the fake had before there was a switch: an
    # add naming a key the playlist already holds makes a SECOND row, and a key
    # repeated inside one uri makes two.
    a, b = _t(1), _t(2)
    pl = FakePlaylist("Mix", [a, b], 500)
    pl.addItems([a])
    assert pl.live_keys() == [1, 2, 1]
    pl.addItems([a, a])
    assert pl.live_keys() == [1, 2, 1, 1, 1]


def test_deduping_server_holds_one_row_per_track() -> None:
    # The other server this app has to survive: the add is answered 200 and
    # changes nothing, whether the key was already on a row or is repeated inside
    # the single comma-joined uri. `add_calls` still records what was SENT — the
    # uri is the thing production is judged on, the effect is the server's.
    a, b, c = _t(1), _t(2), _t(3)
    pl = FakePlaylist("Mix", [a, b], 500, duplicates="dedupe")
    pl.addItems([a])
    assert pl.live_keys() == [1, 2]
    pl.addItems([c, c])
    assert pl.live_keys() == [1, 2, 3]  # the repeat inside ONE uri collapses too
    pl.addItems([a, _t(4)])
    assert pl.live_keys() == [1, 2, 3, 4]  # a present key drops, a new one lands
    assert pl.add_calls == [[1], [3, 3], [1, 4]]


@pytest.mark.parametrize("mode", ["honour", "dedupe"])
def test_seeded_rows_are_server_state_and_are_never_deduped(mode: DuplicateAdds) -> None:
    # The constructor stands for rows the server ALREADY has, not for a request:
    # a copy holding a track twice is exactly the shape the duplicate path has to
    # reconcile, and a deduping fake that refused to hold one could never be
    # pointed at it.
    a = _t(1)
    pl = FakePlaylist("Mix", [a, _t(2), a], 500, duplicates=mode)
    assert pl.live_keys() == [1, 2, 1]


def test_a_deduping_server_collapses_a_repeat_in_the_create_uri() -> None:
    # createPlaylist comma-joins its items into one uri like addItems does, so it
    # carries the same unknown — and the same two answers.
    a, b = _t(1), _t(2)
    honouring = FakeServer([a, b]).createPlaylist("Mix", items=[a, a, b])
    assert honouring.live_keys() == [1, 1, 2]
    deduping = FakeServer([a, b], duplicates="dedupe")
    assert deduping.createPlaylist("Mix", items=[a, a, b]).live_keys() == [1, 2]
    assert deduping.create_calls == [[1, 1, 2]]  # what was sent, not what stuck


def test_a_created_playlist_inherits_the_servers_duplicate_behaviour() -> None:
    # One server behaves one way; a playlist it minted must not answer adds
    # differently from one that was seeded, or a create-path test would pass
    # under a mode it never actually ran.
    server = FakeServer([_t(1), _t(2)], duplicates="dedupe")
    pl = server.createPlaylist("Mix", items=[_t(1)])
    pl.addItems([_t(1), _t(2)])
    assert pl.live_keys() == [1, 2]
    assert server.switchUser("u1")._duplicates == "dedupe"  # and so does every account


def test_a_row_is_deleted_by_its_playlist_item_id() -> None:
    # The whole reason the raw endpoint is there: removeItems resolves a key to
    # its FIRST row, so it cannot address the SECOND row of a duplicated track.
    # The URL is the one plexapi's own removeItems builds (playlist.py:289-290).
    a = _t(1)
    pl = FakePlaylist("Mix", [a, _t(2), a], 500)
    rows = pl.items()
    server = pl._server
    server.query(f"{pl.key}/items/{rows[2].playlistItemID}", method=server._session.delete)
    assert pl.live_keys() == [1, 2]
    assert pl.queries == ["DELETE /playlists/500/items/3"]
    assert pl.calls == []  # it is a server call, not a method on the playlist
    assert [row.playlistItemID for row in pl.items()] == [1, 2, 3]  # cache still stale


def test_a_row_is_moved_by_its_playlist_item_id() -> None:
    # Same for placement: moveItem resolves BOTH arguments by first match, so the
    # second row of a key can neither be moved nor be moved after. The URL is the
    # one moveItem builds (playlist.py:310-313).
    a = _t(1)
    pl = FakePlaylist("Mix", [a, _t(2), a], 500)
    rows = pl.items()
    server = pl._server
    server.query(
        f"{pl.key}/items/{rows[2].playlistItemID}/move?after={rows[0].playlistItemID}",
        method=server._session.put,
    )
    # Read by ROW: both rows here are track 1, so the key list cannot tell
    # "after row 1" from "to the front" — the two land the same keys.
    assert pl.live_row_ids() == [1, 3, 2]
    assert pl.live_keys() == [1, 1, 2]
    server.query(f"{pl.key}/items/{rows[1].playlistItemID}/move", method=server._session.put)
    assert pl.live_row_ids() == [2, 1, 3]  # no `after` = the front
    assert pl.live_keys() == [2, 1, 1]
    assert pl.queries == [
        "PUT /playlists/500/items/3/move?after=1",
        "PUT /playlists/500/items/2/move",
    ]


def test_row_requests_404_on_a_row_the_server_no_longer_has() -> None:
    a, b = _t(1), _t(2)
    pl = FakePlaylist("Mix", [a, b], 500)
    server = pl._server
    gone = pl.items()[1].playlistItemID
    server.query(f"{pl.key}/items/{gone}", method=server._session.delete)
    with pytest.raises(FakeNotFound):
        server.query(f"{pl.key}/items/{gone}", method=server._session.delete)
    with pytest.raises(FakeNotFound):
        server.query(f"{pl.key}/items/{gone}/move", method=server._session.put)
    with pytest.raises(FakeNotFound):  # and the ANCHOR has to be there too
        server.query(f"{pl.key}/items/1/move?after={gone}", method=server._session.put)
    assert pl.live_keys() == [1]  # a refused move moves nothing


def test_moving_a_row_after_itself_is_refused_as_undefined() -> None:
    # By key this is unavoidable on [1, 2, 1] and the fake refuses it; by ROW id
    # it is a request production never has to make, so it stays refused rather
    # than quietly meaning "leave it alone".
    pl = FakePlaylist("Mix", [_t(1), _t(2)], 500)
    server = pl._server
    with pytest.raises(FakeBadRequest):
        server.query(f"{pl.key}/items/1/move?after=1", method=server._session.put)
    assert pl.live_keys() == [1, 2]


def test_row_requests_are_refused_on_a_smart_or_deleted_playlist() -> None:
    smart = FakePlaylist("Smart", [_t(1)], 500, smart=True)
    with pytest.raises(FakeBadRequest):
        smart._server.query(f"{smart.key}/items/1", method=smart._server._session.delete)
    dead = FakePlaylist("Mix", [_t(1)], 501)
    dead.delete()
    with pytest.raises(FakeNotFound):
        dead._server.query(f"{dead.key}/items/1/move", method=dead._server._session.put)


def test_the_fake_refuses_a_request_it_does_not_model() -> None:
    # A fake that quietly accepted an unmodelled URL, verb or session would let
    # production ship a request no real server answers the way this test suite
    # assumed. Each of these is a way to get the shape wrong.
    pl = FakePlaylist("Mix", [_t(1)], 500)
    server = pl._server
    bad: list[Callable[[], object]] = [
        lambda: server.query(f"{pl.key}/items/1"),  # no verb: plexapi defaults to GET
        lambda: server.query(f"{pl.key}/items/1", method="delete"),  # a string, not the session's
        lambda: server.query(f"{pl.key}/items/1", method=server._session.put),  # PUT on delete URL
        lambda: server.query(f"{pl.key}/items/1/move", method=server._session.delete),
        lambda: server.query(f"{pl.key}/items", method=server._session.delete),  # whole list
        lambda: server.query("/playlists/999/items/1", method=server._session.delete),  # not ours
    ]
    for request in bad:
        with pytest.raises(NotImplementedError):
            request()
    assert pl.live_keys() == [1]
    assert pl.queries == []


def test_server_takes_explicit_sections() -> None:
    first, second = FakeSection([], title="Music"), FakeSection([_t(1)], title="MusicDrop")
    server = FakeServer([_t(1)], sections=[first, second])
    assert server.library.sections() == [first, second]


def test_a_section_reports_its_folders_and_never_reports_none() -> None:
    # `LibrarySection.locations` is the list of folder paths Plex holds for the
    # library (library.py:457), and PMS refuses to leave a library with zero of
    # them ("You are unable to remove all locations from a library.",
    # library.py:620-621). So a fake defaulting to an EMPTY list would make the
    # degenerate "Plex reports no folder" case look like the ordinary one, and a
    # UI that only renders folders when it has them would test green while
    # showing the user nothing. A library can legitimately span several folders.
    assert FakeSection([], title="Music").locations == ["/data/music"]
    assert FakeSection([], title="MusicDrop", locations=["/musicdrop", "/mnt/spill"]).locations == [
        "/musicdrop",
        "/mnt/spill",
    ]


def test_switch_user_isolates_playlists_but_shares_library() -> None:
    server = FakeServer([_t(1)])
    user = server.switchUser("u1")
    # The fake hands back one server per uid so the state is shared; real plexapi
    # builds a NEW PlexServer on every call (server.py:269). The sharing is the
    # modelled part, not the identity.
    assert user is server.switchUser("u1")
    user.createPlaylist("Mix", items=[_t(1)])
    assert server.playlists() == []
    assert len(user.playlists()) == 1
    assert user.library.sections()[0] is server.library.sections()[0]


def test_items_returns_the_cached_list_object_itself() -> None:
    # plexapi's items() returns `self._items` (playlist.py:230) — the cached list
    # OBJECT, not a copy. Production that mutates it corrupts the very list
    # _getPlaylistItemID walks, so the fake must let that corruption happen.
    a, b = _t(1), _t(2)
    pl = FakePlaylist("Mix", [a, b], 500)
    assert pl.items() is pl.items()
    pl.items().pop(0)  # production drops a row from the list it was handed
    pl.removeItems([b])
    with pytest.raises(FakeNotFound):  # a is no longer in the list the fake walks
        pl.removeItems([a])
    assert pl.live_keys() == [1]


def test_tracks_and_rows_compare_and_hash_by_rating_key() -> None:
    # plexapi: __eq__ compares `key` (base.py:639-645), so two objects for one
    # track are equal AND hash equal (the fake hashes by key; real hashes repr).
    # A fake comparing by identity hides every `in` / `==` / set() bug.
    assert _t(1).key == "/library/metadata/1"
    assert _t(1) == _t(1)
    assert _t(1) != _t(2)
    assert _t(1) in [_t(1)]
    assert len({_t(1), _t(1), _t(2)}) == 2
    assert (_t(1) == "not a plex object") is False
    pl = FakePlaylist("Mix", [_t(1), _t(1)], 500)
    first, second = pl.items()
    assert first is not second  # distinct rows, as real playlist rows are...
    assert first == second  # ...that compare equal, because the key is the same
    assert first.playlistItemID != second.playlistItemID
    assert first == _t(1)


def test_edit_title_and_summary_stay_stale_until_reload() -> None:
    # plexapi's editField only PUTs (mixins/edit.py:8-30 -> base.py:716-726); it
    # never writes the in-memory attribute, so a re-read still sees the OLD value.
    pl = FakePlaylist("Mix", [_t(1)], 500)
    pl.editTitle("Renamed")
    pl.editSummary("marker")
    assert (pl.title, pl.summary) == ("Mix", "")
    assert (pl.live_title(), pl.live_summary()) == ("Renamed", "marker")
    pl.reload()
    assert (pl.title, pl.summary) == ("Renamed", "marker")


def test_move_after_itself_is_refused_as_undefined() -> None:
    # On [1, 2, 1] both arguments resolve to the SAME first cached row, so the real
    # request is "move row 1 after row 1" — PMS behaviour is undefined. Refuse it
    # loudly rather than invent a NotFound, and leave the playlist untouched.
    a = _t(1)
    pl = FakePlaylist("Mix", [a, _t(2), a], 500)
    with pytest.raises(FakeBadRequest):
        pl.moveItem(a, after=a)
    assert pl.live_keys() == [1, 2, 1]


def test_move_a_row_the_server_already_dropped_raises_not_found() -> None:
    a, b = _t(1), _t(2)
    pl = FakePlaylist("Mix", [a, b], 500)
    pl.items()
    pl.removeItems([a])  # the server drops row #1; the stale cache still lists it
    with pytest.raises(FakeNotFound):
        pl.moveItem(a)
    assert pl.live_keys() == [2]


def test_move_after_a_dropped_row_leaves_the_order_untouched() -> None:
    a, b, c = _t(1), _t(2), _t(3)
    pl = FakePlaylist("Mix", [a, b, c], 500)
    pl.items()
    pl.removeItems([c])
    with pytest.raises(FakeNotFound):
        pl.moveItem(a, after=c)
    assert pl.live_keys() == [1, 2]  # a failed move moves nothing


def test_mutators_on_a_deleted_playlist_raise_not_found() -> None:
    # Once the playlist is gone its key 404s, and plexapi maps 404 -> NotFound
    # (server.py:755-756).
    pl = FakePlaylist("Mix", [_t(1)], 500)
    pl.delete()
    mutators: list[Callable[[], object]] = [
        lambda: pl.addItems([_t(2)]),
        lambda: pl.removeItems([_t(1)]),
        lambda: pl.moveItem(_t(1)),
        lambda: pl.editTitle("Renamed"),
        lambda: pl.editSummary("marker"),
        lambda: pl.uploadPoster(filepath="/tmp/cover.jpg"),
    ]
    for mutate in mutators:
        with pytest.raises(FakeNotFound):
            mutate()
    assert pl.live_keys() == []


def test_add_and_remove_accept_a_tuple_like_plexapi() -> None:
    # plexapi coerces only when the argument is neither list nor tuple
    # (playlist.py:249-250, 285-286).
    a, b = _t(1), _t(2)
    pl = FakePlaylist("Mix", [a], 500)
    pl.addItems((b,))
    assert pl.live_keys() == [1, 2]
    pl.reload()
    pl.removeItems((a,))
    assert pl.live_keys() == [2]


def test_create_playlist_binds_a_second_positional_to_section() -> None:
    # Real is createPlaylist(title, section=None, items=None, ...) (server.py:488),
    # so passing items positionally silently creates nothing.
    server = FakeServer([_t(1)])
    with pytest.raises(FakeBadRequest):
        server.createPlaylist("Mix", [_t(1)])


def test_playlist_keys_come_from_one_server_wide_space() -> None:
    # A playlist ratingKey is a server-global metadata id: an admin playlist and a
    # managed user's playlist can never share one, or a cross-account ratingKey
    # mixup would look like a hit.
    server = FakeServer([_t(1)])
    admin_playlist = server.createPlaylist("Mix", items=[_t(1)])
    user_playlist = server.switchUser("u1").createPlaylist("Mix", items=[_t(1)])
    assert (admin_playlist.ratingKey, user_playlist.ratingKey) == (500, 501)


def test_rows_from_items_are_accepted_back_by_the_mutators() -> None:
    # plexapi passes fetched rows straight back into addItems (Playlist.copyToUser
    # does `create(..., items=self.items())`) and resolves them by ratingKey like
    # any Track — so the fake must take rows wherever it takes tracks.
    a, b = _t(1), _t(2)
    pl = FakePlaylist("Mix", [a, b], 500)
    rows = pl.items()
    pl.addItems(rows)
    assert pl.live_keys() == [1, 2, 1, 2]
    pl.reload()
    assert [r.playlistItemID for r in pl.items()] == [1, 2, 3, 4]  # appended rows are NEW rows
    pl.removeItems([pl.items()[0]])
    assert pl.live_keys() == [2, 1, 2]
