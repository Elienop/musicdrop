"""Which DIRECTORY each Trash-origin call site resolves — pinned per call site.

Every mover takes ``trash_dir`` and ``origins_dir`` as a pair of ``Path``s, and
below the wiring every test passes them explicitly. That makes a whole class of
bug invisible to the unit tests: a call site that resolves the wrong one of the
two, or forgets to pass the second at all. Both fail silently and neither shows
up as an error — records written into the Trash dir land in the entry namespace
the listing walks, and a missing store degrades every row to an import-restore.

A sibling-swap sweep over the origins call sites (``resolve_trash_origins_dir``
-> ``resolve_trash_dir``, and dropping the kwarg where it is optional) found
eight that no test noticed. This file is one pin per survivor, each written as a
BEHAVIOURAL check — a record present where it belongs after the operation, or
gone from where it belonged after an empty — rather than an assertion about the
call.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from beets import config
from beets.library import Item, Library
from fastapi.testclient import TestClient

from app.beets.config_editor import _settings
from app.beets.library import LibraryHandle, _require_id
from app.beets.trash import resolve_trash_dir, resolve_trash_origins_dir
from app.beets.trash_origins import origin_recorded, write_trash_origin
from app.config import Settings
from tests.conftest import beets_dir_for, build_library, make_test_handle, origins_for

SAMPLE = Path(__file__).parent / "fixtures" / "silent.flac"


def _origins_dir(client: TestClient) -> Path:
    """The active origin store, resolved exactly as the routes resolve it.

    Not read off a response: the origins path is deliberately not on the wire
    (adding it would be a contract change for something no UI shows), so a test
    that needs it resolves it the way the routes do. Same helper as
    ``test_trash_api``; duplicated rather than imported so this file states its
    own assumptions.
    """
    app: object = client.app
    handle: LibraryHandle = app.state.beets_library  # type: ignore[attr-defined]  # duck-typed
    return resolve_trash_origins_dir(_settings(app), handle)  # type: ignore[arg-type]  # ditto


def _trash_dir_of(client: TestClient) -> Path:
    app: object = client.app
    handle: LibraryHandle = app.state.beets_library  # type: ignore[attr-defined]  # duck-typed
    return resolve_trash_dir(_settings(app), handle)  # type: ignore[arg-type]  # ditto


def _stub_app(handle: LibraryHandle, trash: Path, origins: Path) -> object:
    """A request whose app carries just the two pieces the ops read."""

    class _App:
        state = SimpleNamespace(
            beets_library=handle,
            settings=Settings(trash_dir=str(trash), trash_origins_dir=str(origins)),
        )

    class _Req:
        app = _App()

    return _Req()


# ----- the re-attach after a config Apply --------------------------------------


def test_config_apply_reattaches_the_trash_origin_store(client: TestClient) -> None:
    """Apply must re-attach BOTH dirs, because a re-attach that omits one CLEARS it.

    ``ImportJobRegistry.attach_library`` assigns every field unconditionally, so
    the kwarg is not "optional, keeps the old value" — leaving it out sets the
    store to ``None``. The Replace pass then returns early (both dirs or
    neither, by design) and every later import silently stops trashing the copy
    it supersedes: two copies on disk, no error anywhere, until the next
    restart. The lifespan side of this is pinned in test_albums.py; this is its
    twin for the only other caller.

    Seeded with a wrong value first, so a pass means Apply WROTE the fresh one
    rather than that nothing touched it.
    """
    from app.import_jobs.registry import get_registry
    from app.main import app

    stale = Path("/stale/origins")
    get_registry().attach_library(app.state.beets_library.lib, Path("/stale/trash"))
    get_registry()._trash_origins_dir = stale

    assert client.post("/api/config/apply").status_code == 200

    handle = app.state.beets_library
    origins = resolve_trash_origins_dir(_settings(app), handle)
    trash = resolve_trash_dir(_settings(app), handle)
    assert get_registry()._trash_origins_dir == origins
    assert get_registry()._trash_dir == trash
    # ...and the two are not the same dir, which is what the sibling swap would
    # make them: a record written into the entry namespace lists as an album.
    assert origins != trash


# ----- the delete fan-out ------------------------------------------------------


def test_delete_artist_op_records_origins_in_the_store_not_in_trash(
    duplicates_lib: Library, tmp_path: Path
) -> None:
    """The artist op resolves the ORIGINS dir; its album twin's pin does not cover it.

    Both ops resolve the pair independently, and swapping this one for
    ``resolve_trash_dir`` writes every artist delete's records INTO Trash —
    where ``list_trashed_albums`` walks the entries, so each record shows up as
    a trashed album of its own AND the real row loses its exact restore.
    """
    from app.beets.delete import delete_artist_op
    from app.beets.trash_manage import list_trashed_albums

    trash = tmp_path / "trash"
    origins = tmp_path / "trash-origins"
    handle = make_test_handle(duplicates_lib, beets_dir_for(tmp_path))
    roots = {
        os.path.dirname(os.fsdecode(next(iter(a.items())).path))
        for a in duplicates_lib.albums()
        if a.albumartist == "Radiohead"
    }
    assert len(roots) == 2, "fixture should hold two Radiohead albums"

    asyncio.run(delete_artist_op(_stub_app(handle, trash, origins), "Radiohead"))  # type: ignore[arg-type]  # stub req

    assert len(list(origins.glob("*.json"))) == 2, "the op resolved no origin store"
    assert not list(trash.glob("*.json")), "records must not land in the entry namespace"
    rows = list_trashed_albums(
        trash, origins_dir=origins, music_dir=os.fsdecode(duplicates_lib.directory)
    )
    assert {r.origin for r in rows} == roots
    assert {r.restore_mode for r in rows} == {"move_back"}


# ----- putting a folder back ---------------------------------------------------


def _tagged_flac(dst: Path, *, artist: str, album: str, title: str) -> None:
    """A real, tagged FLAC — beets must be able to read it as an ``Item``."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SAMPLE, dst)
    item = Item(album=album, albumartist=artist, artist=artist, title=title, track=1)
    item.path = os.fsencode(str(dst))
    item.write()


def _bystander(lib: Library, music: Path) -> None:
    """One album really on disk, so the library does not read as a dropped share.

    A restore writes INTO the music library and so runs behind
    ``require_library_present``. A library whose every album is missing from
    disk IS the dropped-share fixture, and an empty music root is refused by the
    cheaper root check before that — so a restore test built on either would be
    asserting through a guard that should have refused it.
    """
    dst = music / "Bystander" / "Album" / "01 t.flac"
    _tagged_flac(dst, artist="Bystander", album="Album", title="T")
    item = Item(album="Album", albumartist="Bystander", artist="Bystander", title="T", track=1)
    item.path = os.fsencode(str(dst))
    lib.add_album([item]).store()


def test_restore_route_reads_the_record_from_the_origin_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /trash/restore resolves the store on its OWN line, and only this sees it.

    The route hands ``restore_album`` the pair, and every test below the route
    passes them explicitly — so pointing this one at the Trash dir is invisible
    to all of them. Under that swap the record is simply never found: the
    move-back is never offered, and the folder goes back through the import
    fallback to be re-filed by the path template, leaving the record in the
    store for the next folder to take that name. A silent downgrade to the
    pre-origins behaviour, on the restore route of the branch that added
    origins. The STATUS does not give it away either — measured on this
    fixture the swap still answers ``200 restored``, and on a folder that
    duplicates a live album it answers ``200`` with ``restored: false,
    reason: could_not_restore`` — which is why the assertions below are about
    where the files went, not about the response's verdict.

    Pinned on the two halves an exact restore is made of: the folder lands at
    the RECORDED origin (a name the path template would never produce, so
    landing there can only have come from the record), and the record is
    CONSUMED — the entry is gone from Trash, so a record left behind would be
    inherited by whatever takes that name next.

    Built on its own library rather than the ``client`` fixture's, which loads
    the ``musicbrainz`` plugin: this is the only test here that runs a real
    beets import, and against that plugin the restore spends a minute and a half
    waiting on lookups over the network.
    """
    from app.main import app

    config["threaded"] = False  # reset by the autouse _clear_beets_globals fixture
    music = tmp_path / "music"
    lib = build_library(str(tmp_path / "library.db"), str(music))
    _bystander(lib, music)
    trash, origins = tmp_path / "trash", origins_for(tmp_path / "trash")
    entry = trash / "Weird Folder"
    _tagged_flac(entry / "01 Dreams.flac", artist="2 Brothers", album="Dreams", title="Dreams")
    origin = music / "Weird Folder"
    write_trash_origin(origins, entry.name, origin=str(origin), moved="folder")
    # Both dirs pinned inside tmp_path AND at different names, so the route has
    # a real choice to get wrong.
    monkeypatch.setattr(
        app.state, "beets_library", make_test_handle(lib, beets_dir_for(tmp_path)), raising=False
    )
    monkeypatch.setattr(
        app.state,
        "settings",
        Settings(trash_dir=str(trash), trash_origins_dir=str(origins)),
        raising=False,
    )
    client = TestClient(app)

    r = client.post("/api/trash/restore", json={"folder": entry.name})

    assert r.status_code == 200
    assert r.json()["restored"] is True
    assert r.json()["reason"] == "restored"
    assert [p.name for p in origin.glob("*.flac")] == ["01 Dreams.flac"]
    assert not (music / "2 Brothers").exists()  # never re-filed by the path template
    assert not entry.exists()
    assert not origin_recorded(origins, entry.name), "the record outlived the entry"


# ----- emptying Trash ----------------------------------------------------------


def test_empty_one_deletes_the_record_from_the_origin_store(client: TestClient) -> None:
    """A permanent delete must take the record with it — from the STORE.

    Pointed at the Trash dir instead, the deletion silently finds nothing and
    the record outlives the entry it describes. That is not just litter: the
    allocator treats a recorded name as occupied, so the name is burnt forever
    and the next album to earn it is filed as ``<name> (1)``.
    """
    trash = _trash_dir_of(client)
    origins = _origins_dir(client)
    (trash / "Album").mkdir(parents=True)
    write_trash_origin(origins, "Album", origin="/music/Artist/Album", moved="folder")
    assert origin_recorded(origins, "Album")

    assert client.delete("/api/trash", params={"folder": "Album"}).status_code == 200

    assert not origin_recorded(origins, "Album")


def test_empty_all_deletes_every_record_from_the_origin_store(client: TestClient) -> None:
    """Same for the whole-Trash sweep, which resolves the store on its own line."""
    trash = _trash_dir_of(client)
    origins = _origins_dir(client)
    for name in ("A", "B"):
        (trash / name).mkdir(parents=True)
        write_trash_origin(origins, name, origin=f"/music/{name}", moved="folder")

    assert client.delete("/api/trash/all").status_code == 200

    assert not origin_recorded(origins, "A")
    assert not origin_recorded(origins, "B")
    assert not list(origins.glob("*.json"))


# ----- the reorganize orphan sweep ---------------------------------------------


def test_orphan_sweep_records_the_husks_origin_in_the_store(
    reorganize_lib: Library, tmp_path: Path
) -> None:
    """The husk's record is its ONLY exit from Trash, and the sweep writes it.

    An audio-free art/booklet folder cannot be imported, so without the record
    a swept husk can only ever be deleted permanently. The sweep passes the
    store down two hops to ``trash_folder``; handing it the Trash dir instead
    writes the record inside the namespace the listing walks and leaves the row
    itself with nothing to restore to.
    """
    from app.reorganize_jobs.registry import ReorganizeRegistry
    from app.reorganize_jobs.runner import sweep

    music_dir = Path(os.fsdecode(reorganize_lib.directory))
    husk = music_dir / "Ghost Artist"
    husk.mkdir(parents=True, exist_ok=True)
    (husk / "artist-poster.jpg").write_bytes(b"x")
    trash = tmp_path / "trash"
    origins = origins_for(trash)

    reg = ReorganizeRegistry()
    reg.start(scope="library", artist=None, album_id=None, scope_label="library")
    sweep(
        reg,
        make_test_handle(reorganize_lib, beets_dir_for(tmp_path)),
        scope="library",
        trash_dir=trash,
        trash_origins_dir=origins,
    )

    assert reg.state().orphans_trashed == 1
    assert origin_recorded(origins, "Ghost Artist")
    assert not list(trash.glob("*.json"))


def test_the_reorganize_worker_THREAD_is_handed_the_origin_store(
    reorganize_lib: Library, tmp_path: Path
) -> None:
    """``start_backfill`` hands the pair to the sweep on its own line, off-thread.

    Nothing else runs that line. The sweep test above calls ``sweep`` directly,
    and both route tests replace ``start_backfill`` with a fake — so the real
    spawn lambda never executes, and a store swapped for the Trash dir there
    (or a kwarg dropped, which turns the orphan pass off entirely) survives all
    three. It is the last hop before the worker, so getting it wrong sends
    EVERY reorganize's husk records into the entry namespace while the routes
    above it resolve the store perfectly.

    Waits on the runner's own ``on_complete`` rather than polling: it fires in
    ``sweep``'s ``finally``, after the job is finished, so a pass cannot read a
    half-swept Trash dir.
    """
    from app.reorganize_jobs.registry import ReorganizeRegistry
    from app.reorganize_jobs.runner import start_backfill

    music_dir = Path(os.fsdecode(reorganize_lib.directory))
    husk = music_dir / "Ghost Artist"
    husk.mkdir(parents=True, exist_ok=True)
    (husk / "artist-poster.jpg").write_bytes(b"x")
    trash = tmp_path / "trash"
    origins = origins_for(trash)
    done = threading.Event()

    reg = ReorganizeRegistry()
    reg.start(scope="library", artist=None, album_id=None, scope_label="library")
    start_backfill(
        reg,
        make_test_handle(reorganize_lib, beets_dir_for(tmp_path)),
        scope="library",
        trash_dir=trash,
        trash_origins_dir=origins,
        on_complete=done.set,
    )

    assert done.wait(timeout=30), "the reorganize worker never finished"
    assert reg.state().phase == "done", reg.state().error
    assert reg.state().orphans_trashed == 1
    assert origin_recorded(origins, "Ghost Artist")
    assert not list(trash.glob("*.json")), "records must not land in the entry namespace"


def test_album_reorganize_passes_the_trash_origin_store_to_the_worker(
    reorganize_lib: Library, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The album-scope route resolves the pair on its OWN lines.

    Its library-scope sibling is pinned in test_reorganize_api; that pin cannot
    see this route, and a resolve swapped here sends the sweep's records into
    Trash for every single-album reorganize.
    """
    import app.api.reorganize as reorganize_api
    from app.api.albums import get_library
    from app.main import app

    handle = make_test_handle(reorganize_lib, beets_dir_for(tmp_path))
    app.dependency_overrides[get_library] = lambda: handle
    app.state.beets_library = handle
    received: dict[str, object] = {}

    def fake_start_backfill(reg: object, handle: object, **kwargs: object) -> None:
        received.update(kwargs)
        reg.set_total(0)  # type: ignore[attr-defined]  # fake reg is the real registry
        reg.finish("done")  # type: ignore[attr-defined]  # ditto

    monkeypatch.setattr(reorganize_api, "start_backfill", fake_start_backfill)
    album_id = _require_id(next(iter(reorganize_lib.albums())).id)
    try:
        client = TestClient(app)
        assert client.post(f"/api/albums/{album_id}/reorganize").status_code == 200
    finally:
        app.dependency_overrides.clear()

    trash = received["trash_dir"]
    origins = received["trash_origins_dir"]
    assert isinstance(trash, Path)
    assert isinstance(origins, Path)
    assert origins != trash
    assert not origins.is_relative_to(trash), "the store must be a sibling, not a child"
    # ...and the sweep must be told not to trash the store it is writing into.
    assert origins in received["ignore_dirs"]  # type: ignore[operator]  # tuple of Path


# ----- duplicate resolve -------------------------------------------------------


def test_resolve_duplicates_op_records_origins_in_the_store(
    duplicates_lib: Library, tmp_path: Path
) -> None:
    """The single-group resolve op resolves the store on its own line too.

    ``trash_album`` records ``moved="items"`` for each loser it relocates, so a
    resolve that swapped the two dirs would write one record per loser straight
    into Trash — a phantom entry in the listing for every duplicate resolved.
    """
    from app.beets.duplicates import find_duplicate_albums, resolve_duplicates_op
    from app.models.duplicates import DuplicateMode, ResolveRequest

    trash, origins = tmp_path / "trash", tmp_path / "trash-origins"
    report = find_duplicate_albums(duplicates_lib, mode=DuplicateMode.strict)
    group = report.groups[0]
    keep = group.suggested_keeper_id
    losers = [m.id for m in group.members if m.id != keep]
    req = ResolveRequest(mode=DuplicateMode.strict, keep_album_id=keep, remove_album_ids=losers)
    handle = make_test_handle(duplicates_lib, beets_dir_for(tmp_path))

    result = asyncio.run(
        resolve_duplicates_op(_stub_app(handle, trash, origins), req)  # type: ignore[arg-type]  # stub req
    )

    assert len(result.moved) == len(losers)
    assert len(list(origins.glob("*.json"))) == len(losers)
    assert not list(trash.glob("*.json")), "records must not land in the entry namespace"


def test_resolve_all_op_records_origins_in_the_store(
    duplicates_lib: Library, tmp_path: Path
) -> None:
    """The batch op resolves it on a THIRD line, and nothing else covers that one."""
    from app.beets.duplicates import find_duplicate_albums, resolve_all_op
    from app.models.duplicates import DuplicateMode, GroupDecision, ResolveAllRequest

    trash, origins = tmp_path / "trash", tmp_path / "trash-origins"
    report = find_duplicate_albums(duplicates_lib, mode=DuplicateMode.strict)
    decisions = [
        GroupDecision(
            keep_album_id=g.suggested_keeper_id,
            remove_album_ids=[m.id for m in g.members if m.id != g.suggested_keeper_id],
        )
        for g in report.groups
    ]
    req = ResolveAllRequest(mode=DuplicateMode.strict, groups=decisions)
    handle = make_test_handle(duplicates_lib, beets_dir_for(tmp_path))

    result = asyncio.run(
        resolve_all_op(_stub_app(handle, trash, origins), req)  # type: ignore[arg-type]  # stub req
    )

    assert result.moved_count > 0
    assert len(list(origins.glob("*.json"))) == result.moved_count
    assert not list(trash.glob("*.json")), "records must not land in the entry namespace"
