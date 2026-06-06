from pathlib import Path

from app.playlists.m3u import M3uEntry, delete_m3u, render_m3u, write_m3u


def test_render_extm3u_format() -> None:
    entries = [
        M3uEntry(
            duration_seconds=204,
            artist="Radiohead",
            title="Reckoner",
            path="../Radiohead/In Rainbows/07 Reckoner.flac",
        ),
        M3uEntry(
            duration_seconds=251,
            artist="Boards of Canada",
            title="Roygbiv",
            path="../Boards of Canada/Music Has.../Roygbiv.flac",
        ),
    ]
    text = render_m3u("Late night", entries)
    lines = text.split("\n")
    assert lines[0] == "#EXTM3U"
    assert lines[1] == "#PLAYLIST:Late night"
    assert lines[2] == "#EXTINF:204,Radiohead - Reckoner"
    assert lines[3] == "../Radiohead/In Rainbows/07 Reckoner.flac"
    assert text.endswith("\n")


def test_render_empty_playlist() -> None:
    text = render_m3u("Empty", [])
    assert text == "#EXTM3U\n#PLAYLIST:Empty\n"


def test_render_sanitizes_newline_in_name() -> None:
    # A name with an embedded directive must not become its own line.
    text = render_m3u("Evil\n#EXTINF:9,x - y", [])
    assert "\n#EXTINF" not in text
    lines = text.split("\n")
    assert lines[1].startswith("#PLAYLIST:Evil ")


def test_render_sanitizes_crlf_in_metadata() -> None:
    entries = [
        M3uEntry(duration_seconds=100, artist="A\nInjected", title="T\rBad", path="x.flac"),
    ]
    text = render_m3u("Name", entries)
    assert "\r" not in text
    extinf_lines = [ln for ln in text.split("\n") if ln.startswith("#EXTINF")]
    assert len(extinf_lines) == 1  # the artist newline folded inline, no forged line
    assert "Injected" in extinf_lines[0]


def test_write_and_delete(tmp_path: Path) -> None:
    dest = tmp_path / "sub" / "abc.m3u8"
    entries = [M3uEntry(duration_seconds=100, artist="A", title="T", path="../A/t.flac")]
    write_m3u(dest, "Mix", entries)  # creates parent dir
    assert dest.is_file()
    assert "#EXTINF:100,A - T" in dest.read_text(encoding="utf-8")
    delete_m3u(dest)
    assert not dest.exists()
    delete_m3u(dest)  # idempotent, no raise
