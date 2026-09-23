from pathlib import Path
from typing import Final, NamedTuple

from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo root is the parent of the backend/ package dir (this file is
# backend/app/config.py). Relative cache paths resolve under it so the
# artist-image cache lands in the gitignored repo-root data/, not backend/data/.
_REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MUSICDROP_", env_file=".env", extra="ignore")

    app_name: str = "MusicDrop"
    # Shipped builds bake the release tag in via MUSICDROP_VERSION (Docker
    # build-arg -> ENV); everything else (dev checkouts, tests) reads "dev".
    version: str = "dev"
    # beets integration:
    # data/beets is the user-owned BEETSDIR (config.yaml + library.db live here).
    # All library/directory/plugins are read FROM data/beets/config.yaml at startup
    # by app/beets/setup.py; there are no separate MUSICDROP_BEETS_LIBRARY_* knobs.
    beets_dir: str = "data/beets"

    # Built-frontend dir served by FastAPI in the Docker image (set there to
    # /app/static). Empty in dev: the Vite dev server owns the frontend and
    # this seam is a no-op. (env MUSICDROP_STATIC_DIR)
    static_dir: str = ""

    # The single account's password, as a self-describing scrypt hash string
    # (``scrypt$n$r$p$salt$digest`` — see app/auth/passwords.py). Generate one
    # with ``uv run python -m app.auth.hash_password``. There is deliberately no
    # plaintext-password setting: an env var is readable from `docker inspect`,
    # the compose file and every process listing on the box, so what is stored
    # here must already be useless to whoever reads it. Empty = no account is
    # configured and every gated API request is refused. (env
    # MUSICDROP_PASSWORD_HASH)
    password_hash: str = ""

    # DNS names (comma-separated) accepted in the Host header — e.g. the
    # reverse-proxy site name the box is browsed by. IP literals and localhost
    # always pass; every other name is rejected with a 400 (the DNS-rebinding
    # guard — see app/host_guard.py). (env MUSICDROP_ALLOWED_HOSTS)
    allowed_hosts: str = ""

    # Lyrics fetches: a courtesy inter-track pause (backfill and per-album),
    # also the inter-artist pause in the artist-image backfill. beets 2.13
    # separately rate-limits the lyrics HTTP itself (0.25s/request + 429
    # backoff), so this is pacing on top, not the only throttle.
    # (env MUSICDROP_LYRICS_BACKFILL_DELAY_SECONDS)
    lyrics_backfill_delay_seconds: float = 0.2

    # Max request-body size the API accepts (413 above it) — bounds the JSON/YAML/
    # m3u parse endpoints and uploads. (env MUSICDROP_MAX_BODY_BYTES)
    max_body_bytes: int = 25 * 1024 * 1024

    # Duplicate resolution moves the non-kept copies here (a reversible Trash).
    # Empty string = default to <beets_dir>/trash, computed at resolve time from
    # the live library handle (already an absolute path), which sidesteps the
    # cwd-relative gotcha. Set an absolute path to override. (env MUSICDROP_TRASH_DIR)
    #
    # A value INSIDE the music library is allowed and is the reason to set this at
    # all: <music>/.trash makes a delete a same-disk rename. A Trash that IS or
    # CONTAINS the music library, the beets data dir, the database, the origin
    # store or another app-owned store is refused, at startup AND at each
    # destructive use site — a symlink dropped at this path after boot turned Empty
    # Trash into an rmtree of the library. app/beets/store_layout.py holds the
    # table and the sentence.
    trash_dir: str = ""

    # Where each trashed folder's origin record is kept — one JSON file per Trash
    # entry, keyed on the entry's name. A SIBLING of the Trash dir, never inside
    # it: inside trash_dir a record would collide with the entry namespace the
    # listing walks, and Empty Trash would delete the records it needs.
    # Empty string = default to <beets_dir>/trash-origins, computed at resolve
    # time from the live library handle (already absolute), like trash_dir. Set an
    # absolute path to override — one NOT under the music library, because the
    # orphan sweep's whole-folder move (trash_folder) on a folder above the store
    # would take the records into Trash with it.
    # Same table and same call sites as trash_dir (app/beets/store_layout.py).
    # (env MUSICDROP_TRASH_ORIGINS_DIR)
    trash_origins_dir: str = ""

    # Artist images (app/artwork/) — opt-in, conservative defaults.
    artist_images_enabled: bool = False
    artist_image_cache_dir: str = "data/cache/artist-images"
    # Derived album-cover thumbnails (320px WebP) live here — rebuildable cache,
    # safe to delete. (env MUSICDROP_COVER_THUMB_CACHE_DIR)
    cover_thumb_cache_dir: str = "data/cache/cover-thumbs"
    # Deezer allows ~50 req / 5s; stay well under it.
    artist_image_rate_per_sec: float = 5.0
    artist_image_max_concurrency: int = 2
    # How many Deezer search hits to consider before verifying names.
    artist_image_search_limit: int = 5
    # Negative cache: a CONFIRMED no-verified-match is honored this long
    # (seconds) before we re-try. Default ~7 days.
    artist_image_negative_ttl_seconds: int = 7 * 24 * 60 * 60
    # A TRANSIENT failure (429 / timeout / malformed body / bad download) is
    # honored only briefly so a Deezer blip doesn't bench a real artist.
    artist_image_transient_ttl_seconds: int = 600
    # How long an uncached artist-image request waits for its resolve before
    # answering 404 and letting the fill finish in the background. A single
    # artist page therefore still paints inline (~a few hundred ms), while a
    # cold roster page returns immediately instead of holding ~48 requests open
    # for ~10 s behind the 5/s limiter. 0 disables inline serving entirely.
    # (env MUSICDROP_ARTIST_IMAGE_INLINE_GRACE_SECONDS)
    artist_image_inline_grace_seconds: float = 1.5

    # Optional artist-image source credentials (env-only). A source joins the
    # resolution chain only when its credentials are present; with none set the
    # behaviour is exactly Deezer-only. (env MUSICDROP_ARTIST_IMAGE_*)
    artist_image_fanarttv_api_key: str = ""
    artist_image_fanarttv_client_key: str = ""
    artist_image_spotify_client_id: str = ""
    artist_image_spotify_client_secret: str = ""

    # Phase 2: write artist art into the library for Plex (off by default).
    # (env MUSICDROP_ARTIST_ART_WRITE_ENABLED)
    artist_art_write_enabled: bool = False

    # Playlists (app/playlists/) — MusicDrop owns playlist state as JSON files.
    # Empty string = default to <beets_dir>/playlists, computed at resolve time
    # (sidesteps the cwd-relative gotcha when beets_dir is absolute in tests).
    # Set an absolute path to override. (env MUSICDROP_PLAYLISTS_DIR)
    playlists_dir: str = ""

    # Where the Plex-readable .m3u8 exports are written. Empty string = default
    # to <music library>/.playlists (resolved from the live library directory).
    # (env MUSICDROP_PLAYLISTS_EXPORT_DIR)
    playlists_export_dir: str = ""

    # The import bank (app/bank/) — persistent set-aside review queue. Empty
    # string = default to <beets_dir>/bank, resolved at request time like
    # playlists_dir. (env MUSICDROP_BANK_DIR)
    bank_dir: str = ""

    # Plex sync (app/plex/). Empty plex_settings_dir = <beets_dir>/plex.
    # base URL + admin token + the music-library path AS PLEX SEES IT (for the
    # Docker mount difference) + the Plex music-section TITLE (empty works only
    # when the server has a SINGLE artist section — with several, the client
    # refuses until one is named; see app/plex/client.py). All env-seed the
    # persisted JSON config.
    # (env MUSICDROP_PLEX_URL / MUSICDROP_PLEX_TOKEN / MUSICDROP_PLEX_LIBRARY_PATH
    #  / MUSICDROP_PLEX_LIBRARY_SECTION)
    plex_settings_dir: str = ""
    plex_url: str = ""
    plex_token: str = ""
    plex_library_path: str = ""
    plex_library_section: str = ""

    # Acquisition (app/acquisition/). Where completed downloads land before the
    # unattended import; empty = default to <beets_dir>/inbox, computed at resolve
    # time from the live library handle (sidesteps the cwd-relative gotcha, like
    # trash_dir). (env MUSICDROP_INBOX_DIR)
    inbox_dir: str = ""
    # How long an inbox folder must be quiet before "Review inbox" will import
    # it. The inbox IS the downloader's live output dir: slskd moves each file
    # in as it completes, so a folder touched moments ago may still be gaining
    # tracks — importing it then files a PARTIAL album (and the remainder
    # arrives later as a second, duplicate copy).
    # (env MUSICDROP_INBOX_SETTLE_SECONDS)
    inbox_settle_seconds: int = 60

    # slskd acquisition source (app/slskd/). The API key + webhook secret are
    # secrets: these env vars seed the INITIAL persisted JSON config (file > env);
    # the file is stored 0o600 and the secrets are never returned by the API.
    # ``slskd_auto_import`` is the OPERATIVE auto-import toggle — a completed slskd
    # download imports itself only when it is on. Empty slskd_settings_dir =
    # <beets_dir>/slskd. (env MUSICDROP_SLSKD_SETTINGS_DIR / _URL / _TOKEN /
    # _DOWNLOADS_PREFIX / _WEBHOOK_SECRET / _AUTO_IMPORT)
    slskd_settings_dir: str = ""
    slskd_url: str = ""
    slskd_token: str = ""
    slskd_downloads_prefix: str = ""
    slskd_webhook_secret: str = ""
    slskd_auto_import: bool = False


settings = Settings()


def _anchored(value: str) -> Path:
    """A cache path, anchoring a relative value to the repo root."""
    configured = Path(value)
    return configured if configured.is_absolute() else _REPO_ROOT / configured


def resolve_artist_image_cache_dir() -> Path:
    """Resolve the artist-image cache dir, anchoring relatives to the repo root."""
    return _anchored(settings.artist_image_cache_dir)


def resolve_cover_thumb_cache_dir() -> Path:
    """Resolve the cover-thumb cache dir, anchoring relatives to the repo root."""
    return _anchored(settings.cover_thumb_cache_dir)


class AppStore(NamedTuple):
    """One directory MusicDrop owns: the setting that spells it and its default.

    ``key`` is the :class:`Settings` field, ``leaf`` the name it takes under a
    base directory when the setting is empty, ``name`` how a message spells it
    and ``setting`` the env var an operator has to change.
    """

    key: str
    leaf: str
    name: str
    setting: str


BANK_STORE: Final = AppStore("bank_dir", "bank", "the import bank", "MUSICDROP_BANK_DIR")
PLEX_STORE: Final = AppStore(
    "plex_settings_dir", "plex", "the Plex settings store", "MUSICDROP_PLEX_SETTINGS_DIR"
)
SLSKD_STORE: Final = AppStore(
    "slskd_settings_dir", "slskd", "the slskd settings store", "MUSICDROP_SLSKD_SETTINGS_DIR"
)
PLAYLISTS_STORE: Final = AppStore(
    "playlists_dir", "playlists", "the playlist store", "MUSICDROP_PLAYLISTS_DIR"
)
INBOX_STORE: Final = AppStore("inbox_dir", "inbox", "the inbox", "MUSICDROP_INBOX_DIR")
#: Its default sits under the MUSIC library, not the beets dir, so it takes a
#: different base and is not in :data:`APP_STORES`.
EXPORT_STORE: Final = AppStore(
    "playlists_export_dir",
    ".playlists",
    "the playlist exports",
    "MUSICDROP_PLAYLISTS_EXPORT_DIR",
)

#: The five stores that default under the beets data dir.
APP_STORES: Final = (BANK_STORE, PLEX_STORE, SLSKD_STORE, PLAYLISTS_STORE, INBOX_STORE)


def store_dir(settings: Settings, store: AppStore, base: Path) -> Path:
    """Where one app-owned store sits: the configured value, else ``base / leaf``.

    The ONE owner of that formula. Six resolvers each held a copy, and the copies
    diverged: a whitespace-only ``MUSICDROP_INBOX_DIR`` named ``<B>/inbox`` to the
    layout rule and the guard (they stripped) and ``<cwd>/'  '`` to the inbox
    resolver (it tested truthiness), so the two protected a directory the app
    never used. The rule, the guard and the app now read one function.
    """
    configured = str(getattr(settings, store.key)).strip()
    return Path(configured) if configured else base / store.leaf


def app_cache_dirs(app_settings: Settings) -> list[tuple[Path, str, str]]:
    """``(path, what it is, setting)`` for the two rebuildable image caches.

    Not in :data:`APP_STORES`: both ship a non-empty default that anchors to the
    repo root rather than to the beets dir, so :func:`store_dir`'s formula does
    not describe them. They are here because the layout rule and the identity
    guard need them as participants — a Trash pointed at either one booted clean
    and Empty Trash removed it (measured in the review round).
    """
    return [
        (
            _anchored(app_settings.artist_image_cache_dir),
            "the artist-image cache",
            "MUSICDROP_ARTIST_IMAGE_CACHE_DIR",
        ),
        (
            _anchored(app_settings.cover_thumb_cache_dir),
            "the cover-thumbnail cache",
            "MUSICDROP_COVER_THUMB_CACHE_DIR",
        ),
    ]


def app_owned_dirs(app_settings: Settings, beets_dir: Path) -> list[tuple[Path, str, str]]:
    """``(path, what it is, setting)`` for every store the app owns.

    The five under the beets dir plus the two image caches, off the one table and
    the one formula the app's own resolvers call — so the layout rule, the
    identity guard and the app name the same directories.
    """
    return [
        *(
            (store_dir(app_settings, store, beets_dir), store.name, store.setting)
            for store in APP_STORES
        ),
        *app_cache_dirs(app_settings),
    ]


def export_dir(app_settings: Settings, music_dir: Path) -> Path:
    """Where the ``.m3u8`` exports live. Same owner as ``reexport.export_dir_for``."""
    return store_dir(app_settings, EXPORT_STORE, music_dir)
