from pathlib import Path

from app.artwork.toggle import ArtistImageToggle


def test_default_used_when_no_file(tmp_path: Path) -> None:
    t = ArtistImageToggle(tmp_path / "_enabled.json", default=True)
    assert t.is_enabled() is True


def test_set_enabled_flips_in_memory(tmp_path: Path) -> None:
    t = ArtistImageToggle(tmp_path / "_enabled.json", default=False)
    assert t.is_enabled() is False
    assert t.set_enabled(True) is True
    assert t.is_enabled() is True


def test_persisted_value_wins_over_default_on_reload(tmp_path: Path) -> None:
    path = tmp_path / "_enabled.json"
    ArtistImageToggle(path, default=False).set_enabled(True)
    # A fresh instance with the opposite env default must still read True.
    assert ArtistImageToggle(path, default=False).is_enabled() is True


def test_corrupt_file_falls_back_to_default(tmp_path: Path) -> None:
    path = tmp_path / "_enabled.json"
    path.write_text("not json", encoding="utf-8")
    assert ArtistImageToggle(path, default=True).is_enabled() is True


def test_non_bool_value_falls_back_to_default(tmp_path: Path) -> None:
    path = tmp_path / "_enabled.json"
    path.write_text('{"enabled": "yes"}', encoding="utf-8")
    assert ArtistImageToggle(path, default=False).is_enabled() is False


def test_set_creates_missing_parent_dir(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "_enabled.json"
    ArtistImageToggle(path, default=False).set_enabled(True)
    assert path.exists()
