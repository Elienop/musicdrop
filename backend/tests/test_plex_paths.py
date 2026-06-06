from app.plex.paths import translate_path


def test_identity_when_no_plex_root() -> None:
    assert translate_path("/music/A/x.flac", "/music", "") == "/music/A/x.flac"


def test_replaces_root_prefix() -> None:
    assert (
        translate_path("/srv/music/A/x.flac", "/srv/music", "/data/music")
        == "/data/music/A/x.flac"
    )


def test_normalizes_trailing_slash() -> None:
    assert (
        translate_path("/srv/music/A/x.flac", "/srv/music/", "/data/music/")
        == "/data/music/A/x.flac"
    )
