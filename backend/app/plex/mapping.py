"""Resolve playlist tracks to Plex Track objects.

Plex exposes no server-side file-path filter, so we scan the music section's
tracks once and build three indexes from that single pass:

- by file path (``Track.locations``) -> the exact, co-located match,
- by ``(album-artist, title)`` -> the metadata fallback for when MusicDrop's
  beets library and Plex hold separately-organized copies of the same music, and
- by ``(album, title)`` -> the last resort for when Plex's own agent REWROTE the
  artist name, sometimes into a different script (beets ``Wael Kfoury`` vs Plex
  ``وائل كفوري``), which no casefold can bridge.

A track is resolved in that order, and the order is contract: whatever resolves
by path today keeps resolving by path. Ambiguity always refuses -- a track that
cannot be singled out is left missing, never guessed.

The third index carries a hazard the first two do not. Singles have album ==
title, so ``(album, title)`` degenerates into a TITLE-ONLY key for exactly the
tracks that motivated it -- and two artists with a single called "Habibi"
collide there. So no candidate is ever accepted on album and title alone: the
lengths have to agree too and Plex's OWN artist name must not contradict the
one we asked for. See ``_album_match``.

One Plex track answers at most one playlist row: a track a row already resolved
to is off the table for every LATER row that isn't the same library item. See
``_free``.

Every match records WHICH rung resolved it (``PlexMatch.method``) and every miss
its identity and WHY it missed, so the UI can point at the row -- and so a sync
that quietly stopped matching by path is visible as a count rather than as a
flat "ok".
"""

from __future__ import annotations

import unicodedata
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal

from app.models.plex import PlexMatchCounts, PlexMatchMethod, PlexMissingTrack

MissReason = Literal["not_found", "ambiguous"]


@dataclass(frozen=True)
class PlexTrackSpec:
    """One ordered playlist track: the beets item id (so a miss can be pointed
    at), its path as Plex sees it (already translated), plus the metadata used
    to fall back when the path isn't found.

    ``length_seconds`` is the track's length in SECONDS (beets' unit), or
    ``None`` when it is unknown -- Plex reports its own duration in
    MILLISECONDS, so the two are converted where they meet, never here."""

    item_id: int
    path: str
    albumartist: str
    album: str
    title: str
    track: int | None
    length_seconds: float | None


@dataclass(frozen=True)
class PlexMatch:
    """One resolved Plex Track and the rung that resolved it.

    The rung travels WITH the track because it is evidence about the sync, not
    about the track: "22 by path, 6 by album+length" reads as healthy, while the
    same 28 tracks all matched by a fallback means the paths are wrong and only
    luck is holding the sync together."""

    track: Any
    method: PlexMatchMethod


@dataclass(frozen=True)
class PlexResolution:
    """``matches`` are the resolved tracks in playlist order (misses dropped),
    each with the rung that found it; ``missing`` is every spec that resolved to
    nothing, in order."""

    matches: list[PlexMatch]
    missing: list[PlexMissingTrack]

    @property
    def tracks(self) -> list[Any]:
        """The resolved Plex Track objects alone, in playlist order."""
        return [match.track for match in self.matches]

    @property
    def matched_by(self) -> PlexMatchCounts:
        """The per-rung tally, for the sync record.

        Built by NAME rather than field by field, so a rung added to
        ``PlexMatchMethod`` is counted here the moment it gets a field on
        ``PlexMatchCounts`` — and if it never gets one, the equality test over
        the two name sets fails rather than the count silently vanishing."""
        return PlexMatchCounts.model_validate(Counter(match.method for match in self.matches))


# How far two lengths for the SAME recording may sit apart, in seconds. The same
# track in two files disagrees by well under a second (lossy encoders add
# padding; Plex rounds a container duration where beets reads the stream), and a
# remaster or a different rip of one CD track drifts by about a second more. Two
# seconds covers that with room to spare while staying far below the gap that
# separates two DIFFERENT recordings sharing an album and a title -- a radio
# edit, a live take, a remix. Widening it is what re-opens the guessing hole
# this fallback exists to close, so it is deliberately not configurable.
_LENGTH_TOLERANCE_SECONDS = 2.0


def _norm(value: str) -> str:
    """casefold + collapse whitespace + strip — the metadata comparison key."""
    return " ".join(value.casefold().split())


def _attr(track: Any, name: str) -> str:
    value = getattr(track, name, "")
    return value if isinstance(value, str) else ""


def _track_no(track: Any) -> int | None:
    index = getattr(track, "index", None)
    if isinstance(index, int):
        return index
    if isinstance(index, str) and index.isdigit():
        return int(index)
    return None


def _measured_length(value: object) -> float | None:
    """``value`` as a length IN ITS OWN UNIT, or ``None`` when there isn't one.

    Deliberately unit-blind — it is the "is there a length here at all?"
    decision, and its one caller with milliseconds in hand (``_plex_seconds``)
    does the conversion itself. Naming a unit here would read as a promise this
    function cannot keep.

    Zero reads as ABSENT, not as "zero seconds": beets stores 0.0 for a length it
    never read and Plex reports 0 for a file it has not analysed. Taken
    literally, those two zeros agree perfectly — and the whole point of comparing
    lengths is to refuse a candidate whose length we cannot vouch for.
    """
    if not isinstance(value, (int, float)):
        return None
    return float(value) if value > 0 else None


def _plex_seconds(track: Any) -> float | None:
    """``Track.duration`` in SECONDS. Plex reports MILLISECONDS (plexapi casts
    the ``duration`` attrib to int); beets' ``length`` is seconds. The two units
    meet here and nowhere else."""
    millis = _measured_length(getattr(track, "duration", None))
    return None if millis is None else millis / 1000.0


def _lengths_agree(candidate_seconds: float | None, spec_seconds: float) -> bool:
    """True when a candidate's length vouches for it: known, and within
    ``_LENGTH_TOLERANCE_SECONDS`` of the spec's on EITHER side."""
    if candidate_seconds is None:
        return False
    return abs(candidate_seconds - spec_seconds) <= _LENGTH_TOLERANCE_SECONDS


def _script_of(char: str) -> str:
    """The Unicode script ``char`` is written in — "LATIN", "ARABIC", "CJK"...

    Read off the character's Unicode NAME, whose first word IS the script for
    every letter ("ARABIC LETTER ALEF", "LATIN SMALL LETTER E WITH ACUTE"). The
    stdlib exposes no script property, and a real one would be a dependency for
    a question we only ask coarsely: can these two names be compared as text at
    all?
    """
    return unicodedata.name(char, "UNKNOWN").split()[0]


def _shared_scripts(left: str, right: str) -> frozenset[str]:
    """The scripts BOTH names write letters in — empty when they share none."""
    return frozenset(_script_of(c) for c in left if c.isalpha()) & frozenset(
        _script_of(c) for c in right if c.isalpha()
    )


def _name_words(name: str, scripts: frozenset[str]) -> tuple[str, ...]:
    """``name`` as sorted comparison words, keeping only letters in ``scripts``.

    Deliberately forgiving, because Plex's agent rewrites names it considers
    equivalent and refusing those would be worse than useless: diacritics fold
    away ("Beyoncé" -> "beyonce"), punctuation splits words rather than joining
    them, the order is dropped so Plex's inverted "Fitzgerald, Ella" meets
    beets' "Ella Fitzgerald", and a bare "the" goes too, so "Beatles, The",
    "The Beatles" and "Beatles" are all one name. Digits are always kept — they
    are scriptless, and "Blink-182" must not read as "Blink-183".
    """
    kept = "".join(c for c in name if not c.isalpha() or _script_of(c) in scripts)
    folded = unicodedata.normalize("NFKD", kept).casefold()
    spaced = "".join(c if c.isalnum() else " " for c in folded if not unicodedata.combining(c))
    return tuple(sorted(word for word in spaced.split() if word != "the"))


def _artist_contradicts(spec: PlexTrackSpec, candidate: Any) -> bool:
    """True when Plex's OWN artist for ``candidate`` plainly names someone else.

    The ``(album, title)`` rung exists because Plex's agent rewrites the artist,
    so it cannot simply demand that the two names match. But it must not IGNORE
    them either: Plex holding "Nancy Ajram" for a track we asked for under "Wael
    Kfoury" is the server telling us, in a name we can read, that this is a
    different recording — and album, title and a length that happens to land
    within a couple of seconds are not enough to overrule that.

    So the two names are compared only in the alphabet they SHARE. Share none —
    beets' "Wael Kfoury" against Plex's "وائل كفوري" — and there is no textual
    comparison to make, which is precisely the case this rung was built for, so
    it stays out of the way. (A bilingual Plex name, "وائل كفوري (Wael Kfoury)",
    shares Latin, and the Latin halves agree.) Share one and the words have to
    agree, loosely (see ``_name_words``) but really: when in doubt this refuses
    and the row stays missing for the user to fix by hand, because the cost of
    the other mistake is a stranger's recording in their playlist under a sync
    reported "ok".
    """
    plex_artist = _attr(candidate, "grandparentTitle")
    scripts = _shared_scripts(spec.albumartist, plex_artist)
    if not scripts:
        return False
    return _name_words(spec.albumartist, scripts) != _name_words(plex_artist, scripts)


@dataclass(frozen=True)
class _Indexes:
    """What one library scan builds: the three keys a spec is tried against, in
    the order ``resolve_ordered_tracks`` tries them."""

    by_path: dict[str, Any]
    by_artist_title: dict[tuple[str, str], list[Any]]
    by_album_title: dict[tuple[str, str], list[Any]]


def _build_indexes(section: Any) -> _Indexes:
    """ONE scan of the section -> all three lookups."""
    by_path: dict[str, Any] = {}
    by_artist_title: dict[tuple[str, str], list[Any]] = {}
    by_album_title: dict[tuple[str, str], list[Any]] = {}
    for track in section.searchTracks():
        for location in track.locations:
            by_path[location] = track
        title = _norm(_attr(track, "title"))
        by_artist_title.setdefault((_norm(_attr(track, "grandparentTitle")), title), []).append(
            track
        )
        by_album_title.setdefault((_norm(_attr(track, "parentTitle")), title), []).append(track)
    return _Indexes(by_path=by_path, by_artist_title=by_artist_title, by_album_title=by_album_title)


def _free(candidates: Iterable[Any], spec: PlexTrackSpec, claimed: dict[Any, int]) -> list[Any]:
    """``candidates`` minus every track another playlist row already took.

    ``claimed`` maps a ratingKey -> the beets item id that resolved to it. A
    FALLBACK may only hand back a track no other item has claimed: two rows
    resolving to one Plex track puts the same song in the playlist twice, and
    since a fallback is inexact at least one of those two rows is showing the
    user a recording that isn't theirs.

    Claimed by the SAME item is not a collision but a playlist that holds one
    library track twice — a legitimate thing to do, and one the sync goes out of
    its way to support (``sync._unique_key_chunks``), so it must keep resolving
    twice.
    """
    return [c for c in candidates if claimed.get(c.ratingKey, spec.item_id) == spec.item_id]


def _meta_match(
    by_meta: dict[tuple[str, str], list[Any]], spec: PlexTrackSpec, claimed: dict[Any, int]
) -> tuple[Any | None, MissReason | None]:
    """Resolve a path-missed spec by metadata.

    Returns ``(track, None)`` on a match; ``(None, "not_found")`` when the key is
    incomplete (a title-only match is a guess) or no unclaimed candidate exists;
    and ``(None, "ambiguous")`` when candidates exist but album, then track
    number, cannot single one out."""
    key = (_norm(spec.albumartist), _norm(spec.title))
    if not all(key):
        return None, "not_found"
    cands = _free(by_meta.get(key, ()), spec, claimed)
    if not cands:
        return None, "not_found"
    if len(cands) == 1:
        return cands[0], None
    album_pool = [c for c in cands if _norm(_attr(c, "parentTitle")) == _norm(spec.album)]
    pool = album_pool or cands
    if len(pool) == 1:
        return pool[0], None
    if spec.track is not None:
        track_pool = [c for c in pool if _track_no(c) == spec.track]
        if len(track_pool) == 1:
            return track_pool[0], None
    return None, "ambiguous"


def _album_match(
    by_album_title: dict[tuple[str, str], list[Any]], spec: PlexTrackSpec, claimed: dict[Any, int]
) -> tuple[Any | None, MissReason | None]:
    """Resolve an album-artist miss by ``(album, title)`` AGREED ON LENGTH.

    The case this exists for: Plex's agent rewrote the artist into another
    script, so beets' ``Wael Kfoury`` and Plex's ``وائل كفوري`` can never share
    an ``(album-artist, title)`` key — while the album title came through
    untouched.

    The trap, and why every accepted candidate has to agree on length: many of
    those releases are SINGLES, where album == title, so this key collapses into
    a title-only one for exactly the tracks it was built for. Two artists with a
    single called "Habibi" then collide, and if only one of them is in Plex a
    key-only match hands back the WRONG RECORDING and reports the sync "ok". A
    length that agrees is what turns the key from a guess into evidence; a
    length either side does not know is no evidence at all, so it refuses. And
    where Plex DID keep a readable artist name, that name gets a veto —
    ``_artist_contradicts``.

    Returns ``(track, None)`` on the one surviving candidate;
    ``(None, "ambiguous")`` when several survive — the caller leaves the row
    missing rather than picking one; ``(None, "not_found")`` when the key is
    incomplete, the length is unknown, or nothing survives.
    """
    key = (_norm(spec.album), _norm(spec.title))
    length = _measured_length(spec.length_seconds)
    if not all(key) or length is None:
        return None, "not_found"
    pool = [
        cand
        for cand in _free(by_album_title.get(key, ()), spec, claimed)
        if _lengths_agree(_plex_seconds(cand), length) and not _artist_contradicts(spec, cand)
    ]
    if len(pool) == 1:
        return pool[0], None
    return (None, "ambiguous") if pool else (None, "not_found")


def _path_claims(by_path: dict[str, Any], specs: list[PlexTrackSpec]) -> dict[Any, int]:
    """Every track an exact path resolves to, claimed by its item, BEFORE any
    fallback runs.

    A path match is the strongest evidence there is, so it owns its track
    wherever the row sits in the playlist: claiming as we went would let a
    fallback on row 1 take the very track row 40 holds the file for, and the
    playlist would carry that track twice while row 40 looks fine.
    """
    claims: dict[Any, int] = {}
    for spec in specs:
        track = by_path.get(spec.path)
        if track is not None:
            claims.setdefault(track.ratingKey, spec.item_id)
    return claims


def _resolve_one(
    indexes: _Indexes, spec: PlexTrackSpec, claimed: dict[Any, int]
) -> tuple[PlexMatch | None, MissReason | None]:
    """``spec`` against each rung in turn — path, album-artist, album+length."""
    track = indexes.by_path.get(spec.path)
    if track is not None:
        return PlexMatch(track=track, method="path"), None
    track, reason = _meta_match(indexes.by_artist_title, spec, claimed)
    if track is not None:
        return PlexMatch(track=track, method="artist_title"), None
    if reason == "ambiguous":
        # Only a CLEAN miss drops to the album key. "ambiguous" means the
        # album-artist index found this track and could not tell its copies
        # apart — and the album key is WIDER than the one that just refused
        # (it drops the artist constraint), so letting a length pick one of
        # those tied copies would be guessing with a weaker key after a
        # stronger one declined. It would also downgrade an honest "found it
        # twice" into "not found" whenever the lengths were unknown.
        return None, reason
    track, reason = _album_match(indexes.by_album_title, spec, claimed)
    if track is not None:
        return PlexMatch(track=track, method="album_length"), None
    return None, reason


def resolve_ordered_tracks(section: Any, specs: list[PlexTrackSpec]) -> PlexResolution:
    """Resolve ordered specs to Track objects (one library scan), recording how
    each one matched and reporting every miss with its identity and reason."""
    indexes = _build_indexes(section)
    matches: list[PlexMatch] = []
    missing: list[PlexMissingTrack] = []
    claimed = _path_claims(indexes.by_path, specs)
    for spec in specs:
        match, reason = _resolve_one(indexes, spec, claimed)
        if match is None:
            missing.append(
                PlexMissingTrack(
                    item_id=spec.item_id,
                    title=spec.title,
                    albumartist=spec.albumartist,
                    album=spec.album,
                    reason=reason or "not_found",
                )
            )
        else:
            # First claimant wins. Today that reads the same as an assignment —
            # a fallback can't reach a claimed track at all (``_free``) and the
            # path rung's claims are all in before the loop starts — so this
            # states the rule the rungs rely on instead of leaning on them.
            claimed.setdefault(match.track.ratingKey, spec.item_id)
            matches.append(match)
    return PlexResolution(matches=matches, missing=missing)
