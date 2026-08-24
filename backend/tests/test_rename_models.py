"""Contract tests for the artist-rename request validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models.rename import ArtistRenameRequest


def test_new_name_is_stripped() -> None:
    req = ArtistRenameRequest(name="Fayrouz", new_name="  Fairuz  ")
    assert req.new_name == "Fairuz"


def test_blank_new_name_rejected() -> None:
    with pytest.raises(ValidationError):
        ArtistRenameRequest(name="Fayrouz", new_name="   ")


def test_same_name_rejected() -> None:
    with pytest.raises(ValidationError):
        ArtistRenameRequest(name="Fairuz", new_name="Fairuz")


def test_same_name_after_strip_rejected() -> None:
    with pytest.raises(ValidationError):
        ArtistRenameRequest(name="Fairuz", new_name=" Fairuz ")


def test_case_only_change_is_allowed() -> None:
    # A case/accent-only rename is a legitimate fix, not a no-op.
    req = ArtistRenameRequest(name="fairuz", new_name="Fairuz")
    assert req.new_name == "Fairuz"


def test_name_is_not_stripped() -> None:
    # ``name`` is the exact roster identity; a trailing space is part of it.
    req = ArtistRenameRequest(name="Fayrouz ", new_name="Fairuz")
    assert req.name == "Fayrouz "
