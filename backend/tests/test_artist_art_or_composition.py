from pathlib import Path

from app.artwork.toggle import ArtistArtWriteToggle, ArtistImageToggle


def test_or_composition_write_on_enables_fetch(tmp_path: Path) -> None:
    image = ArtistImageToggle(tmp_path / "img.json", default=False)
    write = ArtistArtWriteToggle(tmp_path / "art.json", default=False)

    def is_enabled() -> bool:  # the wired OR-composition (image OR write)
        return image.is_enabled() or write.is_enabled()

    assert is_enabled() is False
    write.set_enabled(True)
    assert is_enabled() is True  # write-on turns fetching on
    write.set_enabled(False)
    image.set_enabled(True)
    assert is_enabled() is True  # images-on still works independently
