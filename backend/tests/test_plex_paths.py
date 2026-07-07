from app.plex.paths import translate_path


def test_identity_when_no_plex_root() -> None:
    assert translate_path("/music/A/x.flac", "/music", "") == "/music/A/x.flac"


def test_replaces_root_prefix() -> None:
    assert (
        translate_path("/srv/music/A/x.flac", "/srv/music", "/data/music") == "/data/music/A/x.flac"
    )


def test_normalizes_trailing_slash() -> None:
    assert (
        translate_path("/srv/music/A/x.flac", "/srv/music/", "/data/music/")
        == "/data/music/A/x.flac"
    )


def test_track_outside_beets_root_passes_through_untranslated() -> None:
    # A track that lives OUTSIDE the beets root (a symlink / foreign mount) can't be
    # rebased — its relative path would start with ".." and climb out of plex_root
    # into a nonsense path. Pass it through unchanged (it simply won't resolve in
    # Plex → missing count) instead of producing "/data/othermount/x.flac".
    assert translate_path("/othermount/x.flac", "/music", "/data/music") == "/othermount/x.flac"
