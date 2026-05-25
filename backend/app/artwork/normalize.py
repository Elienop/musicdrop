"""Pure artist-name normalization for strict match verification.

Used on BOTH sides of the comparison (the query and each candidate result) so
that "exact-after-normalization" can be a simple ``==``. The transform is
deliberately conservative: it folds case/accents/whitespace but preserves
word content, so "The Beatles" and "Beatles" stay distinct.
"""

import re
import unicodedata

_WHITESPACE = re.compile(r"\s+")


def normalize_artist_name(name: str) -> str:
    """Return a canonical form of ``name`` for equality comparison.

    Steps: NFKD decompose + drop combining marks (fold accents), casefold,
    collapse internal whitespace runs to a single space, and trim ends.
    """
    decomposed = unicodedata.normalize("NFKD", name)
    without_marks = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    folded = without_marks.casefold()
    collapsed = _WHITESPACE.sub(" ", folded)
    return collapsed.strip()
