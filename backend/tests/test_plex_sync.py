import pytest

from app.plex import sync
from app.plex.config import PlexConfig
from app.plex.errors import PlexConnectionError, PlexNotConfigured
from app.plex.mapping import PlexTrackSpec


class _FakeTrack:
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


class _FakeSection:
    TYPE = "artist"

    def __init__(self, tracks: list[_FakeTrack], *, title: str = "Music") -> None:
        self._tracks = tracks
        self.title = title

    def searchTracks(self) -> list[_FakeTrack]:
        return self._tracks


class _FakePlaylist:
    def __init__(self, title: str, items: list[_FakeTrack], rating_key: int = 500) -> None:
        self.title = title
        self.ratingKey = rating_key
        self.summary = ""
        self._items = list(items)
        self.deleted = False

    def items(self) -> list[_FakeTrack]:
        return list(self._items)

    def addItems(self, tracks: list[_FakeTrack]) -> None:
        self._items.extend(tracks)

    def removeItems(self, tracks: list[_FakeTrack]) -> None:
        self._items = [t for t in self._items if t not in tracks]

    def editSummary(self, summary: str) -> None:
        self.summary = summary

    def delete(self) -> None:
        self._items = []
        self.deleted = True


class _FakeServer:
    def __init__(self, tracks: list[_FakeTrack]) -> None:
        section = _FakeSection(tracks)
        self.library = type("L", (), {"sections": lambda _self: [section]})()
        self._section = section
        self.created: list[_FakePlaylist] = []
        self._playlists: list[_FakePlaylist] = []
        self._next_key = 500

    def playlists(self) -> list[_FakePlaylist]:
        return self._playlists

    def createPlaylist(self, title: str, items: list[_FakeTrack]) -> _FakePlaylist:
        pl = _FakePlaylist(title, items, self._next_key)
        self._next_key += 1  # every created playlist gets a distinct ratingKey
        self.created.append(pl)
        self._playlists.append(pl)
        return pl


CONFIG = PlexConfig(base_url="http://plex:32400", token="t")


def _patch(monkeypatch: pytest.MonkeyPatch, server: object) -> None:
    monkeypatch.setattr(sync.client, "connect", lambda base_url, token: server)


def _p(path: str) -> PlexTrackSpec:
    """A path-only spec (no metadata) — exercises the exact-path branch."""
    return PlexTrackSpec(path=path, albumartist="", album="", title="", track=None)


def test_creates_playlist_with_matched_tracks(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _FakeServer([_FakeTrack(10, ["/m/a.flac"]), _FakeTrack(20, ["/m/b.flac"])])
    _patch(monkeypatch, server)
    state = sync.sync_playlist(CONFIG, "Mix", [_p("/m/a.flac"), _p("/m/b.flac")], playlist_id="p1")
    assert state.status == "ok"
    assert state.missing == 0
    assert state.rating_key == "500"
    assert [t.ratingKey for t in server.created[0].items()] == [10, 20]
    assert server.created[0].summary == "MusicDrop-id:p1"  # stamped for later identity


def test_partial_when_some_paths_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _FakeServer([_FakeTrack(10, ["/m/a.flac"])])
    _patch(monkeypatch, server)
    state = sync.sync_playlist(
        CONFIG, "Mix", [_p("/m/a.flac"), _p("/m/gone.flac")], playlist_id="p1"
    )
    assert state.status == "partial"
    assert state.missing == 1


class _StampFailPlaylist(_FakePlaylist):
    """A playlist whose id-marker stamp (a separate Plex PUT) fails transiently."""

    def editSummary(self, summary: str) -> None:
        raise Exception("transient stamp failure")


class _StampFailServer(_FakeServer):
    def createPlaylist(self, title: str, items: list[_FakeTrack]) -> _FakePlaylist:
        pl = _StampFailPlaylist(title, items, self._next_key)
        self._next_key += 1
        self.created.append(pl)
        self._playlists.append(pl)
        return pl


def test_stamp_failure_does_not_orphan_the_playlist(monkeypatch: pytest.MonkeyPatch) -> None:
    # editSummary is a SEPARATE Plex PUT after createPlaylist; a transient failure
    # there must NOT fail the whole reconcile. The playlist exists and its
    # ratingKey is returned, so the next sync re-finds it by ratingKey. Failing
    # here (status "failed", rating_key None) would orphan the just-created
    # playlist and duplicate it on every subsequent sync — the very thing the
    # identity marker is meant to prevent.
    server = _StampFailServer([_FakeTrack(10, ["/m/a.flac"])])
    _patch(monkeypatch, server)
    states = sync.sync_playlist_to_targets(
        CONFIG, "Mix", [_p("/m/a.flac")], [], playlist_id="p1", rating_keys={}
    )
    assert states["admin"].status == "ok"
    assert states["admin"].rating_key == "500"
    assert len(server.created) == 1  # created exactly once — not orphaned and re-made


def test_reconciles_existing_playlist_by_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _FakeServer([_FakeTrack(10, ["/m/a.flac"]), _FakeTrack(20, ["/m/b.flac"])])
    existing = _FakePlaylist("Mix", [_FakeTrack(99, ["/m/old.flac"])])
    existing.summary = "MusicDrop-id:p1"  # our marker — found by identity, NOT title
    server._playlists.append(existing)
    _patch(monkeypatch, server)
    sync.sync_playlist(CONFIG, "Mix", [_p("/m/b.flac")], playlist_id="p1")
    # The marked playlist is DELETED then recreated fresh (so we never rely on
    # Plex's emptied-playlist behaviour), and re-stamped with the id marker.
    assert existing.deleted is True
    assert len(server.created) == 1
    assert [t.ratingKey for t in server.created[0].items()] == [20]
    assert server.created[0].summary == "MusicDrop-id:p1"


def test_leaves_same_titled_stranger_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    # A same-titled Plex playlist that is NOT ours (no marker, unknown ratingKey)
    # must survive — reconcile creates a fresh copy instead of stomping it.
    server = _FakeServer([_FakeTrack(20, ["/m/b.flac"])])
    stranger = _FakePlaylist("Mix", [_FakeTrack(99, ["/m/old.flac"])], rating_key=42)
    server._playlists.append(stranger)
    _patch(monkeypatch, server)
    state = sync.sync_playlist(CONFIG, "Mix", [_p("/m/b.flac")], playlist_id="p1")
    assert stranger.deleted is False  # the unrelated same-titled playlist is untouched
    assert len(server.created) == 1
    assert state.rating_key == "500"


def test_empty_when_no_tracks(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _FakeServer([])
    _patch(monkeypatch, server)
    state = sync.sync_playlist(CONFIG, "Mix", [_p("/m/gone.flac")], playlist_id="p1")
    assert state.status == "empty"
    assert state.rating_key is None


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
    server = _FakeServer([_FakeTrack(10, ["/m/a.flac"])])

    user_servers: dict[str, _FakeServer] = {
        "7": _FakeServer([_FakeTrack(10, ["/m/a.flac"])]),
        "8": _FakeServer([_FakeTrack(10, ["/m/a.flac"])]),
    }

    def _switch(uid: str) -> _FakeServer:
        return user_servers[uid]

    server.switchUser = _switch  # type: ignore[attr-defined]
    _patch(monkeypatch, server)

    states = sync.sync_playlist_to_targets(
        CONFIG, "Mix", [_p("/m/a.flac")], ["7", "8"], playlist_id="p1", rating_keys={}
    )
    assert set(states) == {"admin", "7", "8"}
    assert states["admin"].status == "ok"
    assert states["7"].status == "ok"
    # each user server actually got a playlist created, stamped with our marker
    assert len(user_servers["7"].created) == 1
    assert user_servers["7"].created[0].summary == "MusicDrop-id:p1"


def test_fan_out_isolates_a_failing_user(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _FakeServer([_FakeTrack(10, ["/m/a.flac"])])

    def _switch(uid: str) -> _FakeServer:
        if uid == "bad":
            raise RuntimeError("no access")
        return _FakeServer([_FakeTrack(10, ["/m/a.flac"])])

    server.switchUser = _switch  # type: ignore[attr-defined]
    _patch(monkeypatch, server)

    states = sync.sync_playlist_to_targets(
        CONFIG, "Mix", [_p("/m/a.flac")], ["bad", "ok"], playlist_id="p1", rating_keys={}
    )
    assert states["admin"].status == "ok"
    assert states["bad"].status == "failed"
    assert states["bad"].error
    assert states["ok"].status == "ok"


def test_fan_out_not_configured() -> None:
    with pytest.raises(PlexNotConfigured):
        sync.sync_playlist_to_targets(
            PlexConfig(), "Mix", [_p("/m/a.flac")], ["7"], playlist_id="p1", rating_keys={}
        )


def test_sync_metadata_fallback_populates(monkeypatch: pytest.MonkeyPatch) -> None:
    # Plex has the song at a different filename; only metadata bridges it.
    track = _FakeTrack(
        42,
        ["/plex/Adele_19_01_Daydreamer.flac"],
        grandparentTitle="Adele",
        parentTitle="19",
        title="Daydreamer",
        index=1,
    )
    server = _FakeServer([track])
    _patch(monkeypatch, server)
    spec = PlexTrackSpec(
        path="/beets/Adele/19/01 Daydreamer.flac",
        albumartist="Adele",
        album="19",
        title="Daydreamer",
        track=1,
    )
    state = sync.sync_playlist(CONFIG, "Mix", [spec], playlist_id="p1")
    assert state.status == "ok"
    assert state.missing == 0
    assert [t.ratingKey for t in server.created[0].items()] == [42]


def test_delete_on_targets_admin_and_user(monkeypatch: pytest.MonkeyPatch) -> None:
    # Deletion targets the RECORDED ratingKey, not the title, so it can never
    # stomp a same-titled playlist belonging to a different MusicDrop playlist.
    admin_pl = _FakePlaylist("Mix", [_FakeTrack(1, ["/m/a.flac"])])
    admin_pl.ratingKey = 500
    server = _FakeServer([])
    server._playlists.append(admin_pl)

    user7 = _FakeServer([])
    user7_pl = _FakePlaylist("Mix", [_FakeTrack(2, ["/m/b.flac"])])
    user7_pl.ratingKey = 600
    user7._playlists.append(user7_pl)
    server.switchUser = lambda uid: user7  # type: ignore[attr-defined]
    _patch(monkeypatch, server)

    results = sync.delete_playlist_on_targets(CONFIG, {"admin": "500", "7": "600"})
    assert results == {"admin": "deleted", "7": "deleted"}
    assert admin_pl.deleted is True
    assert user7_pl.deleted is True


def test_delete_on_targets_wrong_rating_key_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    # A same-titled playlist with a DIFFERENT ratingKey is left untouched.
    other = _FakePlaylist("Mix", [_FakeTrack(1, ["/m/a.flac"])])
    other.ratingKey = 999
    server = _FakeServer([])
    server._playlists.append(other)
    _patch(monkeypatch, server)
    results = sync.delete_playlist_on_targets(CONFIG, {"admin": "500"})
    assert results == {"admin": "absent"}
    assert other.deleted is False  # the unrelated same-titled playlist survives


def test_delete_on_targets_none_rating_key_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    # A target that never got a playlist (rating_key None) is simply "absent" —
    # no lookup, nothing to remove.
    server = _FakeServer([])
    _patch(monkeypatch, server)
    results = sync.delete_playlist_on_targets(CONFIG, {"admin": None})
    assert results == {"admin": "absent"}


def test_delete_on_targets_isolates_a_failing_account(monkeypatch: pytest.MonkeyPatch) -> None:
    admin_pl = _FakePlaylist("Mix", [_FakeTrack(1, ["/m/a.flac"])])
    admin_pl.ratingKey = 500
    server = _FakeServer([])
    server._playlists.append(admin_pl)

    def _switch(uid: str) -> _FakeServer:
        raise RuntimeError("no access to this user")

    server.switchUser = _switch  # type: ignore[attr-defined]
    _patch(monkeypatch, server)

    results = sync.delete_playlist_on_targets(CONFIG, {"admin": "500", "bad": "700"})
    assert results["admin"] == "deleted"  # the failing user never aborts the others
    assert results["bad"] == "failed"
    assert admin_pl.deleted is True


def test_delete_on_targets_not_configured() -> None:
    with pytest.raises(PlexNotConfigured):
        sync.delete_playlist_on_targets(PlexConfig(), {"admin": "500"})


class _TwoSectionServer(_FakeServer):
    """Two artist sections: 'Music' (first) and 'MusicDrop' (second)."""

    def __init__(self, first: _FakeSection, second: _FakeSection) -> None:
        self._sections = [first, second]
        self.library = type("L", (), {"sections": lambda _self: [first, second]})()
        self.created = []
        self._playlists = []
        self._next_key = 500


def test_sync_uses_the_configured_section(monkeypatch: pytest.MonkeyPatch) -> None:
    wanted = _FakeTrack(1, ["/music/a/b/01 x.mp3"], title="x")
    first = _FakeSection([], title="Music")
    second = _FakeSection([wanted], title="MusicDrop")
    server = _TwoSectionServer(first, second)
    _patch(monkeypatch, server)
    config = PlexConfig(base_url="http://plex:32400", token="t", library_section="musicdrop")
    state = sync.sync_playlist(
        config,
        "P",
        [
            PlexTrackSpec(
                path="/music/a/b/01 x.mp3", albumartist="", album="", title="x", track=None
            )
        ],
        playlist_id="p1",
    )
    assert state.status == "ok"  # resolved against the SECOND (named) section


def test_sync_errors_when_the_configured_section_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _FakeServer([])  # only a section titled "Music"
    _patch(monkeypatch, server)
    config = PlexConfig(base_url="http://plex:32400", token="t", library_section="MusicDrop")
    with pytest.raises(PlexConnectionError) as err:
        sync.sync_playlist(config, "P", [], playlist_id="p1")
    assert "Plex music section 'MusicDrop' not found." in str(err.value)


def test_same_title_playlists_do_not_clobber(monkeypatch: pytest.MonkeyPatch) -> None:
    # Two DIFFERENT MusicDrop playlists share the title "Road Trip". Syncing them
    # against the same Plex account must produce two INDEPENDENT Plex playlists,
    # each reconciled against its own identity — never by title.
    server = _FakeServer([_FakeTrack(10, ["/m/a.flac"]), _FakeTrack(20, ["/m/b.flac"])])
    _patch(monkeypatch, server)

    # Playlist A syncs first (no recorded ratingKey) -> creates Plex #1 w/ A's marker.
    states_a = sync.sync_playlist_to_targets(
        CONFIG, "Road Trip", [_p("/m/a.flac")], [], playlist_id="A", rating_keys={}
    )
    plex_a = server.created[0]
    assert plex_a.summary == "MusicDrop-id:A"

    # Playlist B (same title, different id, no recorded key) -> a SECOND distinct
    # Plex playlist; A's copy is NOT deleted.
    states_b = sync.sync_playlist_to_targets(
        CONFIG, "Road Trip", [_p("/m/b.flac")], [], playlist_id="B", rating_keys={}
    )
    plex_b = server.created[1]
    assert plex_a.deleted is False  # B did not clobber A
    assert plex_b.summary == "MusicDrop-id:B"
    assert states_a["admin"].rating_key != states_b["admin"].rating_key

    # Re-syncing A (now WITH its recorded ratingKey) rebuilds A's OWN playlist
    # (found by ratingKey) and never touches B's.
    sync.sync_playlist_to_targets(
        CONFIG,
        "Road Trip",
        [_p("/m/a.flac")],
        [],
        playlist_id="A",
        rating_keys={"admin": states_a["admin"].rating_key},
    )
    assert plex_a.deleted is True  # A's own copy was rebuilt
    assert plex_b.deleted is False  # B's copy was left alone
    assert server.created[2].summary == "MusicDrop-id:A"
