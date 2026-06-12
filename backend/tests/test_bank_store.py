"""Bank store tests — pure filesystem, no beets, payloads kept None
(ParkedAlbum construction is exercised by its own model/mapping tests)."""

from pathlib import Path

from app.bank import store


def _bank(tmp_path: Path) -> Path:
    return tmp_path / "bank"


def _create(tmp_path: Path, *, folder: str = "/library/A/B", reason: str = "no_match") -> str:
    item = store.create_item(
        _bank(tmp_path),
        folder=folder,
        source="sweep",
        reason=reason,  # type: ignore[arg-type]  # test passes literal strings
        fingerprint="f" * 64,
        artist="Artist",
        album="Album",
    )
    return item.id


def test_create_then_get_roundtrip(tmp_path: Path) -> None:
    item_id = _create(tmp_path)
    item = store.get_item(_bank(tmp_path), item_id)
    assert item is not None
    assert item.status == "needs_review"
    assert item.folder == "/library/A/B"
    assert item.banked_at  # set by the store
    assert (_bank(tmp_path) / f"{item_id}.json").exists()


def test_get_rejects_traversal_ids(tmp_path: Path) -> None:
    _create(tmp_path)
    assert store.get_item(_bank(tmp_path), "../../etc/passwd") is None
    assert store.get_item(_bank(tmp_path), "nope") is None


def test_list_paginates_and_filters(tmp_path: Path) -> None:
    ids = [_create(tmp_path, folder=f"/library/A/{i}") for i in range(5)]
    page = store.list_items(_bank(tmp_path), offset=0, limit=3)
    assert [i.id for i in page] == ids[:3]  # banked_at ascending = FIFO review
    rest = store.list_items(_bank(tmp_path), offset=3, limit=3)
    assert [i.id for i in rest] == ids[3:]
    assert store.count_items(_bank(tmp_path)) == 5
    assert store.list_items(_bank(tmp_path), status="ignored") == []
    assert store.count_items(_bank(tmp_path), status="needs_review") == 5


def test_list_skips_corrupt_rows(tmp_path: Path) -> None:
    _create(tmp_path)
    (_bank(tmp_path) / "garbage.json").write_text("{not json", encoding="utf-8")
    assert len(store.list_items(_bank(tmp_path), offset=0, limit=10)) == 1
