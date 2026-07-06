from app.playlists.m3u_parse import parse_m3u


def test_extinf_artist_title_and_duration() -> None:
    content = (
        "#EXTM3U\n#EXTINF:213,Daft Punk - Around the World\n"
        "/old/Music/Daft Punk/01 Around the World.mp3\n"
    )
    parsed = parse_m3u("Favourites.m3u8", content)
    assert parsed.name == "Favourites"
    (entry,) = parsed.entries
    assert entry.position == 0
    assert entry.artist == "Daft Punk"
    assert entry.title == "Around the World"
    assert entry.duration_seconds == 213.0
    assert entry.path == "/old/Music/Daft Punk/01 Around the World.mp3"
    assert entry.source == "/old/Music/Daft Punk/01 Around the World.mp3"


def test_extinf_without_separator_is_title_only() -> None:
    parsed = parse_m3u("x.m3u", "#EXTINF:100,Bohemian Rhapsody\n/a/b.mp3\n")
    assert parsed.entries[0].artist is None
    assert parsed.entries[0].title == "Bohemian Rhapsody"


def test_bare_path_derives_title_from_stem() -> None:
    parsed = parse_m3u("x.m3u", "C:\\Music\\Queen\\07 - Somebody to Love.flac\n")
    (entry,) = parsed.entries
    assert entry.title == "Somebody to Love"  # track number + separators stripped
    assert entry.artist is None
    assert entry.path == "C:\\Music\\Queen\\07 - Somebody to Love.flac"


def test_negative_or_junk_extinf_duration_is_none() -> None:
    parsed = parse_m3u("x.m3u", "#EXTINF:-1,A - B\n/p.mp3\n#EXTINF:abc,C - D\n/q.mp3\n")
    assert parsed.entries[0].duration_seconds is None
    assert parsed.entries[1].duration_seconds is None


def test_bom_crlf_comments_and_blanks() -> None:
    content = "﻿#EXTM3U\r\n\r\n# a comment\r\n#PLAYLIST:ignored\r\n/a/one.mp3\r\n/b/two.mp3\r\n"
    parsed = parse_m3u("mix.M3U8", content)
    assert [e.path for e in parsed.entries] == ["/a/one.mp3", "/b/two.mp3"]
    assert [e.position for e in parsed.entries] == [0, 1]


def test_file_uri_is_decoded() -> None:
    parsed = parse_m3u("x.m3u", "file:///old/My%20Music/song.mp3\n")
    assert parsed.entries[0].path == "/old/My Music/song.mp3"
