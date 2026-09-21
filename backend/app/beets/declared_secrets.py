"""Every config key the installed beets declares secret, by dotted path.

beets marks a secret with ``view.redact = True`` inside a plugin's
``__init__``, so confuse only knows about it once that plugin has LOADED.
A plugin that is not enabled, failed to import, or was just added by an Apply
has no flag, and its secrets would render in clear. beets keeps no static list
of these (``beet config`` has the same gap), so this table is that list.

Scope is the full path: ``smartplaylist.prefix`` is a secret (it may hold a
username and password), ``bareasc.prefix`` is a query prefix.

``tests/test_declared_secrets.py`` re-derives this set from the installed
beets source on every run, so a beets upgrade that adds or moves one fails.
"""

from __future__ import annotations

from typing import Final

BEETS_DECLARED_SECRETS: Final[frozenset[tuple[str, ...]]] = frozenset(
    {
        ("acoustid", "apikey"),
        ("beatport", "apikey"),
        ("beatport", "apisecret"),
        ("bpd", "password"),
        ("discogs", "apikey"),
        ("discogs", "apisecret"),
        ("discogs", "user_token"),
        ("emby", "apikey"),
        ("emby", "password"),
        ("emby", "userid"),
        ("emby", "username"),
        ("fetchart", "fanarttv_key"),
        ("fetchart", "google_engine"),
        ("fetchart", "google_key"),
        ("fetchart", "lastfm_key"),
        ("kodi", "pwd"),
        ("kodi", "user"),
        ("lastfm", "api_key"),
        ("lastfm", "user"),
        ("listenbrainz", "token"),
        ("lyrics", "genius_api_key"),
        ("lyrics", "google_API_key"),
        ("lyrics", "google_engine_ID"),
        ("lyrics", "translate", "api_key"),
        ("mpd", "password"),
        ("musicbrainz", "pass"),
        ("plex", "token"),
        ("smartplaylist", "prefix"),
        ("spotify", "client_id"),
        ("spotify", "client_secret"),
        ("subsonic", "pass"),
        ("subsonic", "user"),
        ("subsonicplaylist", "password"),
        ("tidal", "client_id"),
    }
)
