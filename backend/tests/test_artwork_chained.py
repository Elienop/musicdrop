import pytest

from app.artwork.chained import ChainedArtistImageSource
from app.artwork.source import ResolvedImage, TransientSourceError

IMG = ResolvedImage(data=b"X", content_type="image/jpeg")


class _Hit:
    def __init__(self, img: ResolvedImage) -> None:
        self.img = img
        self.calls: list[tuple[str, str | None]] = []

    async def resolve(self, name: str, *, mbid: str | None = None) -> ResolvedImage | None:
        self.calls.append((name, mbid))
        return self.img


class _Miss:
    async def resolve(self, name: str, *, mbid: str | None = None) -> ResolvedImage | None:
        return None


class _Transient:
    async def resolve(self, name: str, *, mbid: str | None = None) -> ResolvedImage | None:
        raise TransientSourceError("boom")


@pytest.mark.anyio
async def test_first_hit_wins_and_short_circuits() -> None:
    first, second = _Hit(IMG), _Hit(ResolvedImage(data=b"Y", content_type="image/png"))
    chain = ChainedArtistImageSource([first, second])
    assert await chain.resolve("A", mbid="m") == IMG
    assert first.calls == [("A", "m")]
    assert second.calls == []  # never reached


@pytest.mark.anyio
async def test_continues_past_transient_to_next_hit() -> None:
    hit = _Hit(IMG)
    assert await ChainedArtistImageSource([_Transient(), hit]).resolve("A") == IMG
    assert hit.calls == [("A", None)]


@pytest.mark.anyio
async def test_all_miss_returns_none() -> None:
    assert await ChainedArtistImageSource([_Miss(), _Miss()]).resolve("A") is None


@pytest.mark.anyio
async def test_transient_and_no_success_raises() -> None:
    source = ChainedArtistImageSource([_Transient(), _Miss()])
    with pytest.raises(TransientSourceError):
        await source.resolve("A")


@pytest.mark.anyio
async def test_mbid_passed_through_to_children() -> None:
    hit = _Hit(IMG)
    await ChainedArtistImageSource([hit]).resolve("A", mbid="the-mbid")
    assert hit.calls == [("A", "the-mbid")]
