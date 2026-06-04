from pathlib import Path

from app.artwork.toggle import ArtistArtWriteToggle


def test_default_and_persist(tmp_path: Path) -> None:
    p = tmp_path / "_art_write_enabled.json"
    assert ArtistArtWriteToggle(p, default=False).is_enabled() is False
    ArtistArtWriteToggle(p, default=False).set_enabled(True)
    assert ArtistArtWriteToggle(p, default=False).is_enabled() is True  # persisted wins
