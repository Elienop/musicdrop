"""Tests for the release-identity builder (app/beets/release_identity.py).

It reads provenance via ``getattr`` so it works on both a beets ``Album`` (flex
``data_source`` + fixed ``label``/``country``/``media``/``albumdisambig``) and a
match ``AlbumInfo``. Fakes use SimpleNamespace — no beets, no network.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any


def _obj(**kw: Any) -> SimpleNamespace:
    return SimpleNamespace(**kw)


def test_musicbrainz_identity_and_url() -> None:
    from app.beets.release_identity import release_identity

    ri = release_identity(
        _obj(
            data_source="MusicBrainz",
            label="Warner Bros.",
            country="US",
            media='12" Vinyl',
            albumdisambig="1973",
        ),
        "abc-123",
    )
    assert ri.data_source == "MusicBrainz"
    assert (ri.label, ri.country, ri.media, ri.disambiguation) == (
        "Warner Bros.",
        "US",
        '12" Vinyl',
        "1973",
    )
    assert ri.release_url == "https://musicbrainz.org/release/abc-123"


def test_deezer_url_from_numeric_id() -> None:
    from app.beets.release_identity import release_identity

    ri = release_identity(_obj(data_source="Deezer"), 12345)  # Deezer ids are ints
    assert ri.release_url == "https://www.deezer.com/album/12345"


def test_unknown_source_has_no_url() -> None:
    from app.beets.release_identity import release_identity

    assert release_identity(_obj(data_source="Bandcamp"), "x").release_url is None


def test_missing_id_or_source_yields_no_url() -> None:
    from app.beets.release_identity import release_identity

    assert release_identity(_obj(data_source="MusicBrainz"), None).release_url is None
    assert release_identity(_obj(data_source=None), "abc").release_url is None


def test_blank_fixed_fields_normalize_to_none() -> None:
    # beets fixed fields return "" when unset; coerce to None (and "" id -> no url).
    from app.beets.release_identity import release_identity

    ri = release_identity(_obj(data_source="", label="  ", country="US"), "")
    assert ri.data_source is None and ri.label is None
    assert ri.country == "US"
    assert ri.release_url is None


def test_tolerates_absent_attributes() -> None:
    # An AlbumInfo may not carry every attribute; getattr defaults to None.
    from app.beets.release_identity import release_identity

    ri = release_identity(_obj(data_source="MusicBrainz", label="X"), "id1")
    assert (ri.country, ri.media, ri.disambiguation) == (None, None, None)
    assert ri.label == "X"


def test_tolerates_attrdict_keyerror() -> None:
    # beets AlbumInfo/TrackInfo are AttrDicts: a missing key raises KeyError, not
    # AttributeError, which getattr(default) would NOT swallow.
    from app.beets.release_identity import release_identity

    class _AttrDictish:
        data_source = "MusicBrainz"  # present via normal lookup

        def __getattr__(self, key: str) -> object:
            raise KeyError(key)  # absent keys

    ri = release_identity(_AttrDictish(), "a1")
    assert ri.data_source == "MusicBrainz"
    assert (ri.label, ri.media) == (None, None)
    assert ri.release_url == "https://musicbrainz.org/release/a1"
