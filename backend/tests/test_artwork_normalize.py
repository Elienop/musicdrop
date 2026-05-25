import pytest

from app.artwork.normalize import normalize_artist_name


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ABBA", "abba"),
        ("abba", "abba"),
        ("AbBa", "abba"),
        # accents folded
        ("Beyoncé", "beyonce"),
        ("Sigur Rós", "sigur ros"),
        ("Mötley Crüe", "motley crue"),
        # internal whitespace collapsed, ends trimmed
        ("  The   Beatles  ", "the beatles"),
        ("a-ha", "a-ha"),
        # casefold handles eszett
        ("STRASSE", "strasse"),
    ],
)
def test_normalizes_case_accents_whitespace(raw: str, expected: str) -> None:
    assert normalize_artist_name(raw) == expected


def test_the_x_and_x_stay_different() -> None:
    # The whole point of strict verify: "Beatles" must NOT collapse to
    # "The Beatles".
    assert normalize_artist_name("Beatles") != normalize_artist_name("The Beatles")


def test_empty_and_whitespace_only() -> None:
    assert normalize_artist_name("") == ""
    assert normalize_artist_name("   ") == ""
