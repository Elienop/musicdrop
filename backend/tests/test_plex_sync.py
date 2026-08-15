import random
import zlib
from pathlib import Path

import pytest

from app.models.plex import MISSING_TRACKS_CAP, PlexTargetState
from app.plex import sync
from app.plex.config import PlexConfig
from app.plex.errors import PlexConnectionError, PlexNotConfigured
from app.plex.mapping import PlexTrackSpec
from app.plex.sync import PlexArtwork
from tests.plex_fakes import FakePlaylist, FakeSection, FakeServer, FakeTrack

CONFIG = PlexConfig(base_url="http://plex:32400", token="t")


def _patch(monkeypatch: pytest.MonkeyPatch, server: object) -> None:
    monkeypatch.setattr(sync.client, "connect", lambda base_url, token: server)


def _p(path: str) -> PlexTrackSpec:
    """A path-only spec (no metadata) — exercises the exact-path branch.

    ``item_id`` is a crc32, not ``hash()``: the builtin is salted per process
    (``PYTHONHASHSEED``), so an assertion on a reported miss would pass or fail
    by run.
    """
    return PlexTrackSpec(
        item_id=zlib.crc32(path.encode()) % 10_000,
        path=path,
        albumartist="",
        album="",
        title="",
        track=None,
    )


def _marked(playlist: FakePlaylist, playlist_id: str = "p1") -> FakePlaylist:
    """Seed the MusicDrop id marker the way the SERVER would hold it.

    ``editSummary`` only sends the PUT — the fetched ``summary`` attribute stays
    stale until ``reload()`` — so assigning ``playlist.summary`` would set a
    value the next reload wipes, and a test would pass (or fail) for the wrong
    reason. The seeding calls are then dropped from ``calls`` so a test can
    assert on what the SYNC did to this playlist and nothing else.
    """
    playlist.editSummary(f"MusicDrop-id:{playlist_id}").reload()
    playlist.calls.clear()
    return playlist


def test_creates_playlist_with_matched_tracks(monkeypatch: pytest.MonkeyPatch) -> None:
    server = FakeServer([FakeTrack(10, ["/m/a.flac"]), FakeTrack(20, ["/m/b.flac"])])
    _patch(monkeypatch, server)
    state = sync.sync_playlist(CONFIG, "Mix", [_p("/m/a.flac"), _p("/m/b.flac")], playlist_id="p1")
    assert state.status == "ok"
    assert state.missing == 0
    assert state.rating_key == "500"
    assert server.created[0].live_keys() == [10, 20]
    assert server.created[0].live_summary() == "MusicDrop-id:p1"  # stamped for later identity


def test_partial_when_some_paths_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    server = FakeServer([FakeTrack(10, ["/m/a.flac"])])
    _patch(monkeypatch, server)
    state = sync.sync_playlist(
        CONFIG, "Mix", [_p("/m/a.flac"), _p("/m/gone.flac")], playlist_id="p1"
    )
    assert state.status == "partial"
    assert state.missing == 1


def test_stamp_failure_does_not_orphan_the_playlist(monkeypatch: pytest.MonkeyPatch) -> None:
    # editSummary is a SEPARATE Plex PUT after createPlaylist; a transient failure
    # there must NOT fail the whole reconcile. The playlist exists and its
    # ratingKey is returned, so the next sync re-finds it by ratingKey. Failing
    # here (status "failed", rating_key None) would orphan the just-created
    # playlist and duplicate it on every subsequent sync — the very thing the
    # identity marker is meant to prevent.
    def _boom(self: FakePlaylist, summary: str, locked: bool = True) -> FakePlaylist:
        raise RuntimeError("transient stamp failure")

    monkeypatch.setattr(FakePlaylist, "editSummary", _boom)
    server = FakeServer([FakeTrack(10, ["/m/a.flac"])])
    _patch(monkeypatch, server)
    states = sync.sync_playlist_to_targets(
        CONFIG, "Mix", [_p("/m/a.flac")], [], playlist_id="p1", priors={}
    )
    assert states["admin"].status == "ok"
    assert states["admin"].rating_key == "500"
    assert len(server.created) == 1  # created exactly once — not orphaned and re-made


def test_reconciles_existing_playlist_in_place_by_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    server = FakeServer([FakeTrack(10, ["/m/a.flac"]), FakeTrack(20, ["/m/b.flac"])])
    existing = _marked(server.createPlaylist("Mix", items=[FakeTrack(10, ["/m/a.flac"])]))
    _patch(monkeypatch, server)
    state = sync.sync_playlist(CONFIG, "Mix", [_p("/m/b.flac")], playlist_id="p1")
    # The marked playlist is UPDATED IN PLACE: same ratingKey, no delete, no second create.
    assert existing.deleted is False
    assert len(server.created) == 1
    assert state.rating_key == str(existing.ratingKey)
    assert existing.live_keys() == [20]
    assert existing.reload().summary == "MusicDrop-id:p1"  # edits are stale until reload()


def test_successful_reconcile_keeps_the_prior_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # The recorded key IS the playlist; a successful in-place sync returns it unchanged.
    server = FakeServer([FakeTrack(10, ["/m/a.flac"])])
    existing = _marked(FakePlaylist("Mix", [], 999))
    server._playlists.append(existing)
    _patch(monkeypatch, server)
    state = sync.sync_playlist(
        CONFIG, "Mix", [_p("/m/a.flac")], playlist_id="p1", prior=PlexTargetState(rating_key="999")
    )
    assert state.status == "ok"
    assert state.rating_key == "999"
    assert server.created == []


def test_empty_resolve_never_deletes_the_existing_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    # REGRESSION: a transient resolve miss used to DELETE the Plex playlist and erase the key.
    server = FakeServer([])  # nothing resolves
    existing = _marked(FakePlaylist("Mix", [FakeTrack(10, ["/m/a.flac"])], 999))
    server._playlists.append(existing)
    _patch(monkeypatch, server)
    state = sync.sync_playlist(
        CONFIG, "Mix", [_p("/m/a.flac")], playlist_id="p1", prior=PlexTargetState(rating_key="999")
    )
    assert state.status == "empty"
    assert state.rating_key == "999"  # preserved, so a later delete/retry can still find it
    assert state.missing == 1
    assert existing.deleted is False
    assert existing.live_keys() == [10]  # untouched
    assert existing.calls == []  # NO mutation at all


def test_adds_before_removes_so_playlist_never_empties(monkeypatch: pytest.MonkeyPatch) -> None:
    server = FakeServer([FakeTrack(10, ["/m/a.flac"]), FakeTrack(20, ["/m/b.flac"])])
    existing = _marked(server.createPlaylist("Mix", items=[FakeTrack(10, ["/m/a.flac"])]))
    _patch(monkeypatch, server)
    sync.sync_playlist(CONFIG, "Mix", [_p("/m/b.flac")], playlist_id="p1")
    assert existing.live_keys() == [20]
    assert existing.calls.index("addItems") < existing.calls.index("removeItems")


def test_reorders_in_place_with_moves(monkeypatch: pytest.MonkeyPatch) -> None:
    a, b, c = FakeTrack(1, ["/m/a"]), FakeTrack(2, ["/m/b"]), FakeTrack(3, ["/m/c"])
    server = FakeServer([a, b, c])
    existing = _marked(server.createPlaylist("Mix", items=[a, b, c]))
    _patch(monkeypatch, server)
    state = sync.sync_playlist(
        CONFIG, "Mix", [_p("/m/c"), _p("/m/a"), _p("/m/b")], playlist_id="p1"
    )
    assert state.status == "ok"
    assert existing.live_keys() == [3, 1, 2]
    assert "addItems" not in existing.calls and "removeItems" not in existing.calls
    assert existing.deleted is False


def test_added_track_can_be_moved_to_front(monkeypatch: pytest.MonkeyPatch) -> None:
    # A freshly added row is invisible to moveItem until reload() — pins the reload.
    a, b = FakeTrack(1, ["/m/a"]), FakeTrack(2, ["/m/b"])
    server = FakeServer([a, b])
    existing = _marked(server.createPlaylist("Mix", items=[a]))
    _patch(monkeypatch, server)
    sync.sync_playlist(CONFIG, "Mix", [_p("/m/b"), _p("/m/a")], playlist_id="p1")
    assert existing.live_keys() == [2, 1]


def test_no_op_when_plex_already_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    # A settled playlist costs NOTHING — not even the reload a diff would need.
    # Filtering `calls` down to the three mutators would let the whole diff path
    # run (it issues none of them for an already-equal unique playlist) and still
    # pass, so the early return would be unpinned.
    a, b = FakeTrack(1, ["/m/a"]), FakeTrack(2, ["/m/b"])
    server = FakeServer([a, b])
    existing = _marked(server.createPlaylist("Mix", items=[a, b]))
    _patch(monkeypatch, server)
    sync.sync_playlist(CONFIG, "Mix", [_p("/m/a"), _p("/m/b")], playlist_id="p1")
    assert existing.calls == []


def test_unchanged_duplicate_playlist_is_not_churned(monkeypatch: pytest.MonkeyPatch) -> None:
    # Without the equality short-circuit a duplicate-holding playlist is torn down
    # and rebuilt on EVERY sync (add + a reload/remove pair per existing row), so
    # the no-op case has to be pinned on the duplicate path too.
    a, b = FakeTrack(1, ["/m/a"]), FakeTrack(2, ["/m/b"])
    server = FakeServer([a, b])
    existing = _marked(server.createPlaylist("Mix", items=[a, a, b]))
    _patch(monkeypatch, server)
    sync.sync_playlist(CONFIG, "Mix", [_p("/m/a"), _p("/m/a"), _p("/m/b")], playlist_id="p1")
    assert existing.calls == []
    assert existing.live_keys() == [1, 1, 2]


def test_a_track_added_twice_reconciles_through_the_duplicate_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A user re-adds a track already in the playlist: current is UNIQUE, desired
    # holds a duplicate. Only the desired-side duplicate check routes this away
    # from the diff path, which would ask Plex to move a row after ITSELF.
    a, b = FakeTrack(1, ["/m/a"]), FakeTrack(2, ["/m/b"])
    server = FakeServer([a, b])
    existing = _marked(server.createPlaylist("Mix", items=[a, b]))
    _patch(monkeypatch, server)
    state = sync.sync_playlist(
        CONFIG, "Mix", [_p("/m/a"), _p("/m/a"), _p("/m/b")], playlist_id="p1"
    )
    assert state.status == "ok"
    assert existing.live_keys() == [1, 1, 2]
    assert existing.calls.index("addItems") < existing.calls.index("removeItems")


def test_duplicate_tracks_reconcile_to_the_desired_multiset_and_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a, b = FakeTrack(1, ["/m/a"]), FakeTrack(2, ["/m/b"])
    server = FakeServer([a, b])
    existing = _marked(server.createPlaylist("Mix", items=[a, a, b]))  # 1,1,2
    _patch(monkeypatch, server)
    # Desired: 2, 1, 2  (drops one 'a', adds a second 'b', reorders)
    state = sync.sync_playlist(
        CONFIG, "Mix", [_p("/m/b"), _p("/m/a"), _p("/m/b")], playlist_id="p1"
    )
    assert state.status == "ok"
    assert existing.live_keys() == [2, 1, 2]
    assert existing.deleted is False
    assert state.rating_key == str(existing.ratingKey)


def test_smart_playlist_is_reported_failed_not_deleted(monkeypatch: pytest.MonkeyPatch) -> None:
    server = FakeServer([FakeTrack(10, ["/m/a.flac"])])
    existing = _marked(FakePlaylist("Mix", [], 999, smart=True))
    server._playlists.append(existing)
    _patch(monkeypatch, server)
    state = sync.sync_playlist(
        CONFIG, "Mix", [_p("/m/a.flac")], playlist_id="p1", prior=PlexTargetState(rating_key="999")
    )
    assert state.status == "failed"
    assert state.rating_key == "999"
    assert state.error == (
        "This Plex playlist is a smart playlist; MusicDrop can't update it in place."
    )
    assert existing.deleted is False
    assert server.created == []


def test_rename_propagates_in_place(monkeypatch: pytest.MonkeyPatch) -> None:
    server = FakeServer([FakeTrack(10, ["/m/a.flac"])])
    existing = _marked(server.createPlaylist("Old Name", items=[FakeTrack(10, ["/m/a.flac"])]))
    _patch(monkeypatch, server)
    sync.sync_playlist(CONFIG, "New Name", [_p("/m/a.flac")], playlist_id="p1")
    # editTitle only PUTs; the attribute is stale until reload().
    assert existing.reload().title == "New Name"
    assert len(server.created) == 1


def test_missing_marker_is_restamped_in_place(monkeypatch: pytest.MonkeyPatch) -> None:
    server = FakeServer([FakeTrack(10, ["/m/a.flac"])])
    existing = server.createPlaylist("Mix", items=[FakeTrack(10, ["/m/a.flac"])])  # summary ""
    _patch(monkeypatch, server)
    sync.sync_playlist(
        CONFIG,
        "Mix",
        [_p("/m/a.flac")],
        playlist_id="p1",
        prior=PlexTargetState(rating_key=str(existing.ratingKey)),
    )
    assert existing.reload().summary == "MusicDrop-id:p1"


def test_poster_uploaded_on_create_and_only_when_hash_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    art = tmp_path / "cover.jpg"
    art.write_bytes(b"x")
    server = FakeServer([FakeTrack(10, ["/m/a.flac"])])
    _patch(monkeypatch, server)
    s1 = sync.sync_playlist(
        CONFIG, "Mix", [_p("/m/a.flac")], playlist_id="p1", artwork=PlexArtwork(file=art, hash="h1")
    )
    pl = server.created[0]
    assert pl.poster_uploads == [str(art)]
    assert s1.artwork_hash == "h1"
    # Same hash -> no re-upload (in-place keeps the Plex poster).
    s2 = sync.sync_playlist(
        CONFIG,
        "Mix",
        [_p("/m/a.flac")],
        playlist_id="p1",
        prior=s1,
        artwork=PlexArtwork(file=art, hash="h1"),
    )
    assert pl.poster_uploads == [str(art)]
    assert s2.artwork_hash == "h1"
    # Changed hash -> re-upload.
    s3 = sync.sync_playlist(
        CONFIG,
        "Mix",
        [_p("/m/a.flac")],
        playlist_id="p1",
        prior=s2,
        artwork=PlexArtwork(file=art, hash="h2"),
    )
    assert pl.poster_uploads == [str(art), str(art)]
    assert s3.artwork_hash == "h2"


def test_missing_tracks_are_reported_and_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    server = FakeServer([FakeTrack(10, ["/m/keep.flac"])])
    _patch(monkeypatch, server)
    specs = [_p("/m/keep.flac")] + [
        PlexTrackSpec(
            item_id=i,
            path=f"/m/gone{i}.flac",
            albumartist="A",
            album="B",
            title=f"t{i}",
            track=None,
        )
        for i in range(MISSING_TRACKS_CAP + 5)
    ]
    state = sync.sync_playlist(CONFIG, "Mix", specs, playlist_id="p1")
    assert state.status == "partial"
    assert state.missing == MISSING_TRACKS_CAP + 5  # true total
    assert len(state.missing_tracks) == MISSING_TRACKS_CAP
    assert state.missing_tracks[0].item_id == 0
    assert state.missing_tracks[0].reason == "not_found"


def test_failed_target_carries_prior_key_and_artwork_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    server = FakeServer([FakeTrack(10, ["/m/a.flac"])])

    class _Boom(FakeServer):
        def playlists(self) -> list[FakePlaylist]:
            raise RuntimeError("plex down")

    server.users["u1"] = _Boom([FakeTrack(10, ["/m/a.flac"])])
    _patch(monkeypatch, server)
    states = sync.sync_playlist_to_targets(
        CONFIG,
        "Mix",
        [_p("/m/a.flac")],
        ["u1"],
        playlist_id="p1",
        priors={"u1": PlexTargetState(rating_key="42", artwork_hash="h9")},
    )
    assert states["u1"].status == "failed"
    assert states["u1"].rating_key == "42"
    assert states["u1"].artwork_hash == "h9"


def test_leaves_same_titled_stranger_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    # A same-titled Plex playlist that is NOT ours (no marker, unknown ratingKey)
    # must survive — reconcile creates a fresh copy instead of stomping it.
    server = FakeServer([FakeTrack(20, ["/m/b.flac"])])
    stranger = FakePlaylist("Mix", [FakeTrack(99, ["/m/old.flac"])], 42)
    server._playlists.append(stranger)
    _patch(monkeypatch, server)
    state = sync.sync_playlist(CONFIG, "Mix", [_p("/m/b.flac")], playlist_id="p1")
    assert stranger.deleted is False  # the unrelated same-titled playlist is untouched
    assert len(server.created) == 1
    assert state.rating_key == "500"


def test_empty_when_no_tracks(monkeypatch: pytest.MonkeyPatch) -> None:
    # Nothing resolved AND no Plex copy exists: there is nothing to preserve, and
    # Plex refuses an empty create — so no key is recorded.
    server = FakeServer([])
    _patch(monkeypatch, server)
    state = sync.sync_playlist(CONFIG, "Mix", [_p("/m/gone.flac")], playlist_id="p1")
    assert state.status == "empty"
    assert state.rating_key is None
    assert server.created == []


def test_not_configured() -> None:
    with pytest.raises(PlexNotConfigured):
        sync.sync_playlist(PlexConfig(), "Mix", [_p("/m/a.flac")], playlist_id="p1")


def test_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from requests.exceptions import ConnectionError as ReqConnErr

    def boom(base_url: str, token: str) -> object:
        raise ReqConnErr("no route")

    monkeypatch.setattr(sync.client, "connect", boom)
    with pytest.raises(PlexConnectionError):
        sync.sync_playlist(CONFIG, "Mix", [_p("/m/a.flac")], playlist_id="p1")


def test_unexpected_error_translated(monkeypatch: pytest.MonkeyPatch) -> None:
    # Any non-plexapi/non-requests failure is still surfaced as PlexConnectionError.
    def boom(base_url: str, token: str) -> object:
        raise RuntimeError("kaboom")

    monkeypatch.setattr(sync.client, "connect", boom)
    with pytest.raises(PlexConnectionError):
        sync.sync_playlist(CONFIG, "Mix", [_p("/m/a.flac")], playlist_id="p1")


def test_fan_out_to_admin_and_users(monkeypatch: pytest.MonkeyPatch) -> None:
    server = FakeServer([FakeTrack(10, ["/m/a.flac"])])
    _patch(monkeypatch, server)

    states = sync.sync_playlist_to_targets(
        CONFIG, "Mix", [_p("/m/a.flac")], ["7", "8"], playlist_id="p1", priors={}
    )
    assert set(states) == {"admin", "7", "8"}
    assert states["admin"].status == "ok"
    assert states["7"].status == "ok"
    # each user server actually got a playlist created, stamped with our marker
    assert len(server.users["7"].created) == 1
    assert server.users["7"].created[0].live_summary() == "MusicDrop-id:p1"


class _NoAccessServer(FakeServer):
    """``switchUser`` fails for the one uid we have no access to."""

    def switchUser(self, uid: str) -> FakeServer:
        if uid == "bad":
            raise RuntimeError("no access")
        return super().switchUser(uid)


class _SwitchFailsServer(FakeServer):
    """Every ``switchUser`` hits a transient network failure."""

    def switchUser(self, uid: str) -> FakeServer:
        raise RuntimeError("transient network blip")


class _FlakySwitchServer(FakeServer):
    """``switchUser`` fails while ``broken``, then hands back ``stand_in``."""

    broken: bool = True
    stand_in: FakeServer | None = None

    def switchUser(self, uid: str) -> FakeServer:
        if self.broken:
            raise RuntimeError("transient network blip")
        if self.stand_in is None:
            return super().switchUser(uid)
        return self.stand_in


def test_fan_out_isolates_a_failing_user(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _NoAccessServer([FakeTrack(10, ["/m/a.flac"])])
    _patch(monkeypatch, server)

    states = sync.sync_playlist_to_targets(
        CONFIG, "Mix", [_p("/m/a.flac")], ["bad", "ok"], playlist_id="p1", priors={}
    )
    assert states["admin"].status == "ok"
    assert states["bad"].status == "failed"
    assert states["bad"].error
    assert states["ok"].status == "ok"


def test_failed_reconcile_carries_forward_prior_rating_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A transient reconcile failure must NOT erase a target's recorded ratingKey.
    # The caller replaces the WHOLE state map with what we return, so dropping the
    # key here would orphan the still-existing Plex copy: a later delete/de-target
    # short-circuits on a None key and never removes it. Carry the prior key
    # forward so a retry (or delete) can still find the copy by identity.
    server = _SwitchFailsServer([FakeTrack(10, ["/m/a.flac"])])
    _patch(monkeypatch, server)

    states = sync.sync_playlist_to_targets(
        CONFIG,
        "Mix",
        [_p("/m/a.flac")],
        ["7"],
        playlist_id="p1",
        priors={"admin": PlexTargetState(), "7": PlexTargetState(rating_key="600")},
    )
    assert states["7"].status == "failed"
    assert states["7"].rating_key == "600"  # preserved, NOT erased to None


def test_delete_with_none_rating_key_falls_back_to_id_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A record whose ratingKey was lost (e.g. erased by a prior transient-failure
    # bug) but whose Plex copy still exists under our id marker must STILL be
    # deleted — not short-circuited to "absent" and orphaned on Plex forever.
    orphan = _marked(FakePlaylist("Mix", [FakeTrack(1, ["/m/a.flac"])], 777))
    server = FakeServer([])
    server._playlists.append(orphan)
    _patch(monkeypatch, server)
    results = sync.delete_playlist_on_targets(CONFIG, {"admin": None}, playlist_id="p1")
    assert results == {"admin": "deleted"}
    assert orphan.deleted is True


def test_carried_forward_key_lets_a_later_delete_remove_the_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # End-to-end of the fix: a target fails to sync (its prior key is preserved),
    # then is unticked/deleted — the delete finds the copy by the carried-forward
    # ratingKey instead of short-circuiting to "absent" and orphaning it.
    user7 = FakeServer([FakeTrack(2, ["/m/b.flac"])])
    user7_pl = FakePlaylist("Mix", [FakeTrack(2, ["/m/b.flac"])], 600)
    user7._playlists.append(user7_pl)

    server = _FlakySwitchServer([FakeTrack(10, ["/m/a.flac"])])
    server.stand_in = user7
    _patch(monkeypatch, server)

    states = sync.sync_playlist_to_targets(
        CONFIG,
        "Mix",
        [_p("/m/a.flac")],
        ["7"],
        playlist_id="p1",
        priors={"7": PlexTargetState(rating_key="600")},
    )
    assert states["7"].rating_key == "600"  # key survived the failure

    # Now the user is unticked; switchUser works again and delete uses the key.
    server.broken = False
    results = sync.delete_playlist_on_targets(
        CONFIG, {"7": states["7"].rating_key}, playlist_id="p1"
    )
    assert results == {"7": "deleted"}
    assert user7_pl.deleted is True


def test_fan_out_not_configured() -> None:
    with pytest.raises(PlexNotConfigured):
        sync.sync_playlist_to_targets(
            PlexConfig(), "Mix", [_p("/m/a.flac")], ["7"], playlist_id="p1", priors={}
        )


def test_sync_metadata_fallback_populates(monkeypatch: pytest.MonkeyPatch) -> None:
    # Plex has the song at a different filename; only metadata bridges it.
    track = FakeTrack(
        42,
        ["/plex/Adele_19_01_Daydreamer.flac"],
        grandparentTitle="Adele",
        parentTitle="19",
        title="Daydreamer",
        index=1,
    )
    server = FakeServer([track])
    _patch(monkeypatch, server)
    spec = PlexTrackSpec(
        item_id=7,
        path="/beets/Adele/19/01 Daydreamer.flac",
        albumartist="Adele",
        album="19",
        title="Daydreamer",
        track=1,
    )
    state = sync.sync_playlist(CONFIG, "Mix", [spec], playlist_id="p1")
    assert state.status == "ok"
    assert state.missing == 0
    assert server.created[0].live_keys() == [42]


def test_delete_on_targets_admin_and_user(monkeypatch: pytest.MonkeyPatch) -> None:
    # Deletion targets the RECORDED ratingKey, not the title, so it can never
    # stomp a same-titled playlist belonging to a different MusicDrop playlist.
    admin_pl = FakePlaylist("Mix", [FakeTrack(1, ["/m/a.flac"])], 500)
    server = FakeServer([])
    server._playlists.append(admin_pl)

    user7 = FakeServer([])
    user7_pl = FakePlaylist("Mix", [FakeTrack(2, ["/m/b.flac"])], 600)
    user7._playlists.append(user7_pl)
    server.users["7"] = user7
    _patch(monkeypatch, server)

    results = sync.delete_playlist_on_targets(
        CONFIG, {"admin": "500", "7": "600"}, playlist_id="p1"
    )
    assert results == {"admin": "deleted", "7": "deleted"}
    assert admin_pl.deleted is True
    assert user7_pl.deleted is True


def test_delete_on_targets_wrong_rating_key_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    # A same-titled playlist with a DIFFERENT ratingKey is left untouched.
    other = FakePlaylist("Mix", [FakeTrack(1, ["/m/a.flac"])], 999)
    server = FakeServer([])
    server._playlists.append(other)
    _patch(monkeypatch, server)
    results = sync.delete_playlist_on_targets(CONFIG, {"admin": "500"}, playlist_id="p1")
    assert results == {"admin": "absent"}
    assert other.deleted is False  # the unrelated same-titled playlist survives


def test_delete_falls_back_to_id_marker_when_rating_key_is_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # After a Plex DB rebuild the recorded ratingKey changes, so lookup by the
    # RECORDED ratingKey ("500") misses. The playlist still carries our id marker,
    # so we re-find and delete it by marker instead of orphaning the copy.
    stale = _marked(FakePlaylist("Mix", [FakeTrack(1, ["/m/a.flac"])], 999))
    server = FakeServer([])
    server._playlists.append(stale)
    _patch(monkeypatch, server)
    results = sync.delete_playlist_on_targets(CONFIG, {"admin": "500"}, playlist_id="p1")
    assert results == {"admin": "deleted"}
    assert stale.deleted is True


def test_delete_on_targets_none_rating_key_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    # A target that never got a playlist (rating_key None) is simply "absent" —
    # no lookup, nothing to remove.
    server = FakeServer([])
    _patch(monkeypatch, server)
    results = sync.delete_playlist_on_targets(CONFIG, {"admin": None}, playlist_id="p1")
    assert results == {"admin": "absent"}


def test_delete_on_targets_isolates_a_failing_account(monkeypatch: pytest.MonkeyPatch) -> None:
    admin_pl = FakePlaylist("Mix", [FakeTrack(1, ["/m/a.flac"])], 500)
    server = _NoAccessServer([])
    server._playlists.append(admin_pl)
    _patch(monkeypatch, server)

    results = sync.delete_playlist_on_targets(
        CONFIG, {"admin": "500", "bad": "700"}, playlist_id="p1"
    )
    assert results["admin"] == "deleted"  # the failing user never aborts the others
    assert results["bad"] == "failed"
    assert admin_pl.deleted is True


def test_delete_on_targets_not_configured() -> None:
    with pytest.raises(PlexNotConfigured):
        sync.delete_playlist_on_targets(PlexConfig(), {"admin": "500"}, playlist_id="p1")


def test_delete_prefers_our_marked_copy_over_a_stale_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # The recorded key now resolves to a playlist belonging to a DIFFERENT
    # MusicDrop playlist (a Plex DB rebuild reassigned ratingKeys). Deleting what
    # the key points at would destroy a stranger's playlist AND leave ours behind.
    stranger = _marked(FakePlaylist("Mix", [FakeTrack(1, ["/m/a.flac"])], 500), "OTHER")
    ours = _marked(FakePlaylist("Mix", [FakeTrack(2, ["/m/b.flac"])], 777), "p1")
    server = FakeServer([])
    server._playlists.extend([stranger, ours])
    _patch(monkeypatch, server)
    results = sync.delete_playlist_on_targets(CONFIG, {"admin": "500"}, playlist_id="p1")
    assert results == {"admin": "deleted"}
    assert ours.deleted is True
    assert stranger.deleted is False
    assert stranger.calls == []


def test_delete_leaves_a_foreign_marked_playlist_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    # Same stale key, but we have no copy left to delete: report "absent" rather
    # than remove somebody else's playlist.
    stranger = _marked(FakePlaylist("Mix", [FakeTrack(1, ["/m/a.flac"])], 500), "OTHER")
    server = FakeServer([])
    server._playlists.append(stranger)
    _patch(monkeypatch, server)
    results = sync.delete_playlist_on_targets(CONFIG, {"admin": "500"}, playlist_id="p1")
    assert results == {"admin": "absent"}
    assert stranger.deleted is False
    assert stranger.calls == []


def test_delete_still_adopts_an_unmarked_playlist_by_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # An UNMARKED playlist at the recorded key is our own copy whose stamp PUT
    # failed transiently — still ours to delete. Only a FOREIGN marker disqualifies.
    unmarked = FakePlaylist("Mix", [FakeTrack(1, ["/m/a.flac"])], 500)  # summary ""
    server = FakeServer([])
    server._playlists.append(unmarked)
    _patch(monkeypatch, server)
    results = sync.delete_playlist_on_targets(CONFIG, {"admin": "500"}, playlist_id="p1")
    assert results == {"admin": "deleted"}
    assert unmarked.deleted is True


def test_sync_uses_the_configured_section(monkeypatch: pytest.MonkeyPatch) -> None:
    wanted = FakeTrack(1, ["/music/a/b/01 x.mp3"], title="x")
    first = FakeSection([], title="Music")
    second = FakeSection([wanted], title="MusicDrop")
    server = FakeServer([wanted], sections=[first, second])
    _patch(monkeypatch, server)
    config = PlexConfig(base_url="http://plex:32400", token="t", library_section="musicdrop")
    state = sync.sync_playlist(
        config,
        "P",
        [
            PlexTrackSpec(
                item_id=1,
                path="/music/a/b/01 x.mp3",
                albumartist="",
                album="",
                title="x",
                track=None,
            )
        ],
        playlist_id="p1",
    )
    assert state.status == "ok"  # resolved against the SECOND (named) section


def test_sync_errors_when_the_configured_section_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = FakeServer([])  # only a section titled "Music"
    _patch(monkeypatch, server)
    config = PlexConfig(base_url="http://plex:32400", token="t", library_section="MusicDrop")
    with pytest.raises(PlexConnectionError) as err:
        sync.sync_playlist(config, "P", [], playlist_id="p1")
    assert "Plex music section 'MusicDrop' not found." in str(err.value)


def test_same_title_playlists_do_not_clobber(monkeypatch: pytest.MonkeyPatch) -> None:
    # Two DIFFERENT MusicDrop playlists share the title "Road Trip". Syncing them
    # against the same Plex account must produce two INDEPENDENT Plex playlists,
    # each reconciled against its own identity — never by title.
    server = FakeServer([FakeTrack(10, ["/m/a.flac"]), FakeTrack(20, ["/m/b.flac"])])
    _patch(monkeypatch, server)

    # Playlist A syncs first (no recorded ratingKey) -> creates Plex #1 w/ A's marker.
    states_a = sync.sync_playlist_to_targets(
        CONFIG, "Road Trip", [_p("/m/a.flac")], [], playlist_id="A", priors={}
    )
    plex_a = server.created[0]
    assert plex_a.live_summary() == "MusicDrop-id:A"

    # Playlist B (same title, different id, no recorded key) -> a SECOND distinct
    # Plex playlist; A's copy is NOT deleted.
    states_b = sync.sync_playlist_to_targets(
        CONFIG, "Road Trip", [_p("/m/b.flac")], [], playlist_id="B", priors={}
    )
    plex_b = server.created[1]
    assert plex_a.deleted is False  # B did not clobber A
    assert plex_b.live_summary() == "MusicDrop-id:B"
    assert states_a["admin"].rating_key != states_b["admin"].rating_key

    # Re-syncing A (now WITH its recorded ratingKey) updates A's OWN playlist
    # in place (found by ratingKey) and never touches B's.
    states_a2 = sync.sync_playlist_to_targets(
        CONFIG,
        "Road Trip",
        [_p("/m/a.flac")],
        [],
        playlist_id="A",
        priors={"admin": PlexTargetState(rating_key=states_a["admin"].rating_key)},
    )
    assert plex_a.deleted is False  # A's own copy was UPDATED, not recreated
    assert plex_b.deleted is False  # B's copy was left alone
    assert len(server.created) == 2  # no third playlist
    assert states_a2["admin"].rating_key == states_a["admin"].rating_key


def test_uploads_poster_when_artwork_given(monkeypatch: pytest.MonkeyPatch) -> None:
    server = FakeServer([FakeTrack(10, ["/m/a.flac"])])
    _patch(monkeypatch, server)
    art = Path("/data/playlists/artwork/p1.jpg")
    state = sync.sync_playlist(
        CONFIG, "Mix", [_p("/m/a.flac")], playlist_id="p1", artwork=PlexArtwork(file=art, hash="h1")
    )
    assert state.status == "ok"
    assert server.created[0].poster_uploads == [str(art)]


def test_no_poster_upload_when_artwork_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    server = FakeServer([FakeTrack(10, ["/m/a.flac"])])
    _patch(monkeypatch, server)
    state = sync.sync_playlist(CONFIG, "Mix", [_p("/m/a.flac")], playlist_id="p1")
    assert state.status == "ok"
    assert server.created[0].poster_uploads == []  # no art => no upload attempted
    assert state.artwork_hash is None


def test_poster_upload_via_fan_out(monkeypatch: pytest.MonkeyPatch) -> None:
    # The sync endpoint drives the fan-out entry point, so the poster path must
    # thread through it too.
    server = FakeServer([FakeTrack(10, ["/m/a.flac"])])
    _patch(monkeypatch, server)
    art = Path("/data/playlists/artwork/p1.png")
    states = sync.sync_playlist_to_targets(
        CONFIG,
        "Mix",
        [_p("/m/a.flac")],
        [],
        playlist_id="p1",
        priors={},
        artwork=PlexArtwork(file=art, hash="h1"),
    )
    assert states["admin"].status == "ok"
    assert states["admin"].artwork_hash == "h1"
    assert server.created[0].poster_uploads == [str(art)]


def test_poster_upload_failure_leaves_status_ok_and_is_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # uploadPoster is a SEPARATE best-effort Plex PUT; a transient failure there
    # must NOT fail the reconcile — the playlist and its tracks are already
    # synced. The hash is recorded ONLY when the PUT landed, so the next sync
    # retries instead of skipping the poster forever on an unchanged hash.
    def _boom(
        self: FakePlaylist, url: str | None = None, filepath: str | None = None
    ) -> FakePlaylist:
        raise RuntimeError("transient poster failure")

    server = FakeServer([FakeTrack(10, ["/m/a.flac"])])
    _patch(monkeypatch, server)
    art = Path("/data/playlists/artwork/p1.jpg")
    artwork = PlexArtwork(file=art, hash="h1")
    # Scoped so ONLY the poster recovers below — a bare monkeypatch.undo() would
    # also drop the connect patch this test still depends on.
    with pytest.MonkeyPatch.context() as broken_poster:
        broken_poster.setattr(FakePlaylist, "uploadPoster", _boom)
        state = sync.sync_playlist(
            CONFIG, "Mix", [_p("/m/a.flac")], playlist_id="p1", artwork=artwork
        )
    assert state.status == "ok"
    assert state.rating_key == "500"
    assert state.artwork_hash is None  # not recorded, so the next sync tries again

    retry = sync.sync_playlist(
        CONFIG, "Mix", [_p("/m/a.flac")], playlist_id="p1", prior=state, artwork=artwork
    )
    assert server.created[0].poster_uploads == [str(art)]  # retried and landed
    assert retry.artwork_hash == "h1"


def test_a_settled_playlist_is_not_restamped_or_renamed(monkeypatch: pytest.MonkeyPatch) -> None:
    # A fetched playlist carries Plex's CURRENT title and summary, so once a copy
    # is stamped a repeat sync must cost no PUT at all — not the marker, not the
    # title. (Only visible because the fake's playlists() re-fetches like the real
    # server's does; against stale objects every sync re-stamps unnoticed.)
    server = FakeServer([FakeTrack(10, ["/m/a.flac"])])
    _patch(monkeypatch, server)
    first = sync.sync_playlist(CONFIG, "Mix", [_p("/m/a.flac")], playlist_id="p1")
    created = server.created[0]
    created.calls.clear()
    sync.sync_playlist(CONFIG, "Mix", [_p("/m/a.flac")], playlist_id="p1", prior=first)
    assert created.calls == []


def test_a_stale_key_never_hijacks_a_stranger_carrying_a_foreign_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A Plex DB rebuild reassigns ratingKeys, so a recorded key can resolve to a
    # playlist belonging to a DIFFERENT MusicDrop playlist. Adopting it would
    # rewrite that playlist in place AND re-record its key, so every later sync
    # keeps rewriting it while our own copy is orphaned forever.
    server = FakeServer([FakeTrack(10, ["/m/a.flac"]), FakeTrack(20, ["/m/b.flac"])])
    stranger = _marked(FakePlaylist("Mix", [FakeTrack(20, ["/m/b.flac"])], 900), "OTHER")
    ours = _marked(FakePlaylist("Mix", [], 777), "p1")
    server._playlists.extend([stranger, ours])
    _patch(monkeypatch, server)
    state = sync.sync_playlist(
        CONFIG, "Mix", [_p("/m/a.flac")], playlist_id="p1", prior=PlexTargetState(rating_key="900")
    )
    assert state.rating_key == "777"  # OUR copy, found by its marker
    assert ours.live_keys() == [10]
    assert stranger.live_keys() == [20]  # untouched
    assert stranger.calls == []
    assert server.created == []


def test_a_stale_key_pointing_at_a_foreign_marker_creates_a_fresh_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Same hijack, but we have no copy left to find: create a fresh one rather
    # than adopt somebody else's.
    server = FakeServer([FakeTrack(10, ["/m/a.flac"])])
    stranger = _marked(FakePlaylist("Mix", [FakeTrack(20, ["/m/b.flac"])], 900), "OTHER")
    server._playlists.append(stranger)
    _patch(monkeypatch, server)
    state = sync.sync_playlist(
        CONFIG, "Mix", [_p("/m/a.flac")], playlist_id="p1", prior=PlexTargetState(rating_key="900")
    )
    assert len(server.created) == 1
    assert state.rating_key == str(server.created[0].ratingKey)
    assert server.created[0].live_keys() == [10]
    assert stranger.live_keys() == [20]
    assert stranger.calls == []
    assert stranger.deleted is False


def test_rotating_one_track_costs_one_move(monkeypatch: pytest.MonkeyPatch) -> None:
    # Only what actually MOVED may be moved: dragging one row of a 10-row playlist
    # to the end is one PUT, not nine. On a 500-track playlist the naive walk is
    # 499 sequential PUTs, and a failure part-way leaves Plex half-reordered.
    tracks = [FakeTrack(key, [f"/m/{key}"]) for key in range(1, 11)]
    server = FakeServer(tracks)
    existing = _marked(server.createPlaylist("Mix", items=tracks))
    _patch(monkeypatch, server)
    order = [*range(2, 11), 1]
    sync.sync_playlist(CONFIG, "Mix", [_p(f"/m/{key}") for key in order], playlist_id="p1")
    assert existing.live_keys() == order
    assert existing.calls.count("moveItem") == 1


def test_reversing_five_tracks_costs_at_most_four_moves(monkeypatch: pytest.MonkeyPatch) -> None:
    # The worst case is still bounded by N-1 (one row can always stay put).
    tracks = [FakeTrack(key, [f"/m/{key}"]) for key in range(1, 6)]
    server = FakeServer(tracks)
    existing = _marked(server.createPlaylist("Mix", items=tracks))
    _patch(monkeypatch, server)
    sync.sync_playlist(
        CONFIG, "Mix", [_p(f"/m/{key}") for key in range(5, 0, -1)], playlist_id="p1"
    )
    assert existing.live_keys() == [5, 4, 3, 2, 1]
    assert existing.calls.count("moveItem") <= 4


def test_reconcile_lands_on_the_desired_rows_for_arbitrary_shapes() -> None:
    # Property sweep over add/remove/reorder/duplicate shapes at once: whatever
    # the playlist holds and whatever is wanted, the rows end up EXACTLY the
    # desired multiset in the desired order. Seeded, so a failure is replayable.
    rng = random.Random(1234)
    pool = [FakeTrack(key, [f"/m/{key}"]) for key in range(1, 9)]
    for _ in range(300):
        current = [rng.choice(pool) for _ in range(rng.randrange(0, 7))]
        desired = [rng.choice(pool) for _ in range(rng.randrange(1, 7))]
        playlist = FakePlaylist("Mix", current, 500)
        sync._reconcile_items(playlist, desired)
        expected = [track.ratingKey for track in desired]
        assert playlist.live_keys() == expected, f"{[t.ratingKey for t in current]} -> {expected}"
        assert all(len(set(keys)) == len(keys) for keys in playlist.add_calls)


def test_no_plex_call_ever_carries_the_same_key_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    # plexapi comma-joins one addItems/createPlaylist into a single
    # /library/metadata/2,1,2 uri. Whether PMS honours the repeat is unknowable
    # from here, so we never ask: each call carries a key at most once.
    a, b = FakeTrack(1, ["/m/a"]), FakeTrack(2, ["/m/b"])
    server = FakeServer([a, b])
    existing = _marked(server.createPlaylist("Mix", items=[a, a, b]))
    _patch(monkeypatch, server)
    sync.sync_playlist(CONFIG, "Mix", [_p("/m/b"), _p("/m/a"), _p("/m/b")], playlist_id="p1")
    assert existing.live_keys() == [2, 1, 2]
    assert existing.add_calls  # it really went through the add path
    assert all(len(set(keys)) == len(keys) for keys in existing.add_calls)


def test_creating_a_playlist_with_a_repeat_splits_the_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Same hazard on the create path: createPlaylist takes the first occurrence of
    # each key, and the repeats are appended afterwards.
    a, b = FakeTrack(1, ["/m/a"]), FakeTrack(2, ["/m/b"])
    server = FakeServer([a, b])
    _patch(monkeypatch, server)
    state = sync.sync_playlist(
        CONFIG, "Mix", [_p("/m/a"), _p("/m/a"), _p("/m/b")], playlist_id="p1"
    )
    assert state.status == "ok"
    assert server.create_calls == [[1, 2]]
    assert server.created[0].live_keys() == [1, 1, 2]
    assert all(len(set(keys)) == len(keys) for keys in server.created[0].add_calls)


def test_a_server_that_silently_drops_an_add_fails_loudly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # If the rows after the reload are not what we asked for, say so. Walking the
    # desired order against a short list would IndexError into the generic
    # "Plex sync failed." and hide which step went wrong.
    def _swallow(self: FakePlaylist, items: object) -> FakePlaylist:
        self.calls.append("addItems")
        return self  # the PUT "succeeded" and changed nothing

    server = FakeServer([FakeTrack(10, ["/m/a.flac"]), FakeTrack(20, ["/m/b.flac"])])
    existing = _marked(server.createPlaylist("Mix", items=[FakeTrack(10, ["/m/a.flac"])]))
    monkeypatch.setattr(FakePlaylist, "addItems", _swallow)
    _patch(monkeypatch, server)
    with pytest.raises(PlexConnectionError) as err:
        sync.sync_playlist(CONFIG, "Mix", [_p("/m/a.flac"), _p("/m/b.flac")], playlist_id="p1")
    assert "Plex did not apply the playlist changes." in str(err.value)
    assert existing.live_keys() == [10]
