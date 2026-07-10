"""Read Plex audio playlists as import sources.

Server-level reads (playlists are not section-scoped): the user's OLD library
playlists are exactly the point. Paths ride along verbatim for basename
matching against the beets library — no translation. plexapi access stays in
app/plex/ (adapter boundary); ``client.connect`` is the patchable seam.
"""

from __future__ import annotations

from typing import Any, Literal

from plexapi.exceptions import PlexApiException
from requests.exceptions import RequestException

from app.models.playlist_import import ParsedPlaylist, SourceEntry
from app.models.plex import PlexPlaylistInfo
from app.plex import client as client  # explicit re-export: the patchable seam
from app.plex.config import PlexConfig
from app.plex.errors import PlexConnectionError, PlexNotConfigured


def _require(config: PlexConfig) -> None:
    if not (config.base_url and config.token):
        raise PlexNotConfigured("Plex is not configured.")


def _audio_playlists(server: Any) -> list[Any]:
    return [pl for pl in server.playlists() if str(getattr(pl, "playlistType", "")) == "audio"]


def list_audio_playlists(config: PlexConfig) -> list[PlexPlaylistInfo]:
    _require(config)
    try:
        server = client.connect(config.base_url, config.token)
        return [
            PlexPlaylistInfo(name=str(pl.title), track_count=len(pl.items()))
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


def pull_playlist_entries(config: PlexConfig, names: list[str]) -> list[ParsedPlaylist]:
    """The selected playlists' entries, in playlist + track order.

    An unknown name raises ``PlexConnectionError`` (the listing the user
    picked from is stale) rather than silently skipping.
    """
    _require(config)
    try:
        server = client.connect(config.base_url, config.token)
        by_name = {str(pl.title): pl for pl in _audio_playlists(server)}
        missing = [name for name in names if name not in by_name]
        if missing:
            raise PlexConnectionError(f"Plex playlist(s) not found: {', '.join(sorted(missing))}")
        out: list[ParsedPlaylist] = []
        for name in names:
            items = by_name[name].items()
            out.append(
                ParsedPlaylist(
                    name=name,
                    entries=[_entry(i, name, item) for i, item in enumerate(items)],
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


def download_poster(
    config: PlexConfig, playlist_title: str
) -> tuple[bytes, Literal["jpg", "png"]] | None:
    """Fetch the poster of the audio playlist titled ``playlist_title`` (exact
    match) through the server's authed session.

    Returns the raw bytes + sniffed format, or ``None`` when the playlist is
    absent, has no thumb, or the thumb isn't a JPEG/PNG. A genuine Plex/network
    error raises ``PlexConnectionError`` (same as the other reads here) — the
    import commit treats the whole pull as best-effort and swallows either way.
    """
    _require(config)
    try:
        server = client.connect(config.base_url, config.token)
        playlist = next(
            (pl for pl in _audio_playlists(server) if str(pl.title) == playlist_title),
            None,
        )
        if playlist is None:
            return None
        thumb = getattr(playlist, "thumb", None)
        if not thumb:
            return None
        response = server._session.get(server.url(str(thumb), includeToken=True))
        response.raise_for_status()
        data: bytes = response.content
        fmt = _sniff_poster_format(data)
        return (data, fmt) if fmt is not None else None
    except (PlexApiException, RequestException) as exc:
        raise PlexConnectionError("Couldn't read the Plex playlist poster.") from exc
