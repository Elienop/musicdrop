"""Tests for the lyrics adapter (app/beets/lyrics.py) and its models."""

from __future__ import annotations


def test_album_lyrics_result_model_roundtrips() -> None:
    from app.models.lyrics import AlbumLyricsResult, ItemLyricsOutcome

    result = AlbumLyricsResult(
        album_id=7,
        fetched=1,
        not_found=1,
        failed=0,
        skipped=2,
        items=[
            ItemLyricsOutcome(item_id=1, status="found", source="lrclib", written=True),
            ItemLyricsOutcome(item_id=2, status="not_found", source=None, written=False),
        ],
        writes_enabled=True,
    )
    assert result.fetched == 1
    assert result.items[0].status == "found"
    assert result.model_dump()["items"][1]["status"] == "not_found"
