"""Read Plex audio playlists as import sources.

Server-level reads (playlists are not section-scoped): the user's OLD library
playlists are exactly the point. Paths ride along verbatim for basename
matching against the beets library — no translation. plexapi access stays in
app/plex/ (adapter boundary); ``client.connect`` is the patchable seam.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from plexapi.exceptions import PlexApiException
from requests.exceptions import RequestException

from app.models.playlist_import import ParsedPlaylist, SourceEntry
from app.models.plex import PlexPlaylistInfo
from app.plex import client as client  # explicit re-export: the patchable seam
from app.plex.config import PlexConfig
from app.plex.errors import PlexConnectionError, PlexNotConfigured

logger = logging.getLogger(__name__)


def _require(config: PlexConfig) -> None:
    if not (config.base_url and config.token):
        raise PlexNotConfigured("Plex is not configured.")


def _audio_playlists(server: Any) -> list[Any]:
    return [pl for pl in server.playlists() if str(getattr(pl, "playlistType", "")) == "audio"]


def list_audio_playlists(config: PlexConfig) -> list[PlexPlaylistInfo]:
    _require(config)
    try:
        server = client.connect(config.base_url, config.token)
        # ``leafCount`` rides along on the initial server.playlists() response, so
        # the name+count picker needs ZERO extra requests. ``pl.items()`` (a full
        # per-playlist track fetch — many MB over a NAS) stays in
        # pull_playlist_entries, where the contents are actually consumed.
        return [
            PlexPlaylistInfo(
                name=str(pl.title),
                track_count=int(getattr(pl, "leafCount", 0) or 0),
                # The playlist's IDENTITY. Plex allows duplicate titles freely,
                # so the picker (and every later lookup) travels by ratingKey.
                rating_key=str(pl.ratingKey),
            )
            for pl in _audio_playlists(server)
        ]
    except (PlexApiException, RequestException) as exc:
        raise PlexConnectionError("Couldn't read Plex playlists.") from exc


def _entry(position: int, playlist_name: str, item: Any) -> SourceEntry:
    ms = getattr(item, "duration", None)
    locations = list(getattr(item, "locations", None) or [])
    return SourceEntry(
        position=position,
        path=str(locations[0]) if locations else None,
        artist=str(getattr(item, "grandparentTitle", "") or "") or None,
        title=str(getattr(item, "title", "") or "") or None,
        album=str(getattr(item, "parentTitle", "") or "") or None,
        duration_seconds=float(ms) / 1000.0 if ms else None,
        source=f"plex:{playlist_name}",
    )


def pull_playlist_entries(config: PlexConfig, rating_keys: list[str]) -> list[ParsedPlaylist]:
    """The selected playlists' entries, in requested-playlist + track order.

    Selection is by Plex ``ratingKey``, never by title: Plex allows duplicate
    titles, and a title-keyed map silently collapses them (the later one wins,
    the earlier can never be pulled). The returned ``ParsedPlaylist.name`` is
    still the playlist's TITLE — display is unchanged.

    An unknown key raises ``PlexConnectionError`` (the listing the user picked
    from is stale) rather than silently skipping.
    """
    _require(config)
    try:
        server = client.connect(config.base_url, config.token)
        by_key = {str(pl.ratingKey): pl for pl in _audio_playlists(server)}
        missing = [key for key in rating_keys if key not in by_key]
        if missing:
            # Name the COUNT and the remedy, never the raw ratingKeys: this
            # string is surfaced verbatim to the user, and an opaque numeric id
            # tells them nothing about which playlist went away or what to do.
            count = len(missing)
            noun = "playlist" if count == 1 else "playlists"
            raise PlexConnectionError(
                f"{count} selected Plex {noun} no longer on the server — "
                "refresh the list and try again."
            )
        out: list[ParsedPlaylist] = []
        for key in rating_keys:
            playlist = by_key[key]
            title = str(playlist.title)
            items = playlist.items()
            out.append(
                ParsedPlaylist(
                    name=title,
                    entries=[_entry(i, title, item) for i, item in enumerate(items)],
                )
            )
        return out
    except PlexConnectionError:
        raise
    except (PlexApiException, RequestException) as exc:
        raise PlexConnectionError("Couldn't read Plex playlists.") from exc


def _sniff_poster_format(data: bytes) -> Literal["jpg", "png"] | None:
    """The poster's image format from its magic bytes — JPEG or PNG only, else None."""
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    return None


def _fetch_image(server: Any, key: str) -> tuple[bytes, Literal["jpg", "png"]] | None:
    """GET ``key`` through the server's authed session and sniff its format.

    Returns the bytes + format, or ``None`` when the bytes aren't a JPEG/PNG.
    """
    response = server._session.get(server.url(key, includeToken=True))
    response.raise_for_status()
    data: bytes = response.content
    fmt = _sniff_poster_format(data)
    return (data, fmt) if fmt is not None else None


def _selected_poster_bytes(
    server: Any, playlist: Any, label: str
) -> tuple[bytes, Literal["jpg", "png"]] | None:
    """The user's chosen custom poster, if any — degrades to ``None`` on failure.

    plexapi's ``playlist.posters()`` (PosterMixin) lists the custom/agent
    posters; the active one has ``selected=True`` and a fetchable ``key``. A
    ``posters()`` failure, no selected entry, a falsy key, or bytes that fail the
    JPEG/PNG sniff all fall through to ``None`` so the pull degrades to the
    composite mosaic instead of aborting. ``label`` is for logs only.
    """
    try:
        posters = playlist.posters()
    except Exception:  # best-effort: any posters() failure degrades to the composite mosaic
        logger.debug("posters() failed for playlist %r; using composite", label, exc_info=True)
        return None
    selected = next((p for p in posters if getattr(p, "selected", False)), None)
    if selected is None:
        logger.debug("no selected custom poster for playlist %r", label)
        return None
    key = getattr(selected, "key", None)
    if not key:
        logger.debug("selected poster has no key for playlist %r", label)
        return None
    result = _fetch_image(server, str(key))
    if result is None:
        logger.debug("selected poster for playlist %r wasn't JPEG/PNG; using composite", label)
    return result


def _find_playlist(server: Any, rating_key: str | None, playlist_title: str | None) -> Any | None:
    """The audio playlist to pull art from — by ``ratingKey`` when one is known.

    The key is identity: an unknown key means that playlist is gone, and falling
    back to the title there would happily hand back a same-titled STRANGER's
    art. The title match (first exact hit) exists only for records stored before
    keys were carried, which have no key at all.
    """
    playlists = _audio_playlists(server)
    if rating_key:
        return next((pl for pl in playlists if str(pl.ratingKey) == rating_key), None)
    if playlist_title:
        return next((pl for pl in playlists if str(pl.title) == playlist_title), None)
    return None


def download_poster(
    config: PlexConfig,
    rating_key: str | None,
    playlist_title: str | None = None,
) -> tuple[bytes, Literal["jpg", "png"]] | None:
    """Fetch the poster of the audio playlist identified by ``rating_key``
    through the server's authed session.

    ``playlist_title`` is the legacy fallback only — used when no key is
    available (a request minted before keys were carried); when a key IS given
    it decides alone.

    Prefers the user's selected custom poster (``playlist.posters()``); only
    when none is set does it fall back to the composite mosaic. Returns the raw
    bytes + sniffed format, or ``None`` when the playlist is absent, has no
    usable poster, or the image isn't a JPEG/PNG. A genuine Plex/network error
    raises ``PlexConnectionError`` (same as the other reads here) — the import
    commit treats the whole pull as best-effort and swallows either way.
    """
    _require(config)
    label = rating_key or playlist_title or ""
    try:
        server = client.connect(config.base_url, config.token)
        playlist = _find_playlist(server, rating_key, playlist_title)
        if playlist is None:
            logger.debug("no audio playlist %r for poster pull", label)
            return None
        custom = _selected_poster_bytes(server, playlist, label)
        if custom is not None:
            return custom
        # No custom poster selected — fall back to plexapi's ``thumb``, which is
        # a property alias for ``composite`` (Plex's auto-generated 2x2 mosaic).
        thumb = getattr(playlist, "thumb", None)
        if not thumb:
            logger.debug("playlist %r has no composite/thumb", label)
            return None
        result = _fetch_image(server, str(thumb))
        if result is None:
            logger.debug("composite/thumb for playlist %r wasn't JPEG/PNG", label)
        return result
    except (PlexApiException, RequestException) as exc:
        raise PlexConnectionError("Couldn't read the Plex playlist poster.") from exc
