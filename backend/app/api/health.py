"""Liveness, and — separately — which build is running.

The two are split because only one of them is anonymous. ``/api/health`` is on
``app/auth/gate.py::EXEMPT_PATHS`` so the container's HEALTHCHECK can reach it
without a cookie, which means everything in its body is readable by anyone who
can reach the port. It used to carry the running version, handing an
unauthenticated caller the exact build to go look up known issues for; that
moved to ``/api/version``, which is gated like every other ``/api/`` route.
"""

from fastapi import APIRouter
from pydantic import BaseModel

from app.config import settings

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: str


class VersionResponse(BaseModel):
    version: str


@router.get("/health")
async def health() -> HealthResponse:
    """Alive or not — nothing else, because this answer is public.

    Anything added to this body is added to the anonymous attack surface. The
    image's HEALTHCHECK only reads the HTTP status code, so it needs no field
    here at all; ``status`` exists for a human running ``curl``.
    """
    return HealthResponse(status="ok")


@router.get("/version")
async def version() -> VersionResponse:
    """The running build, for the UI's version badge.

    Gated by being absent from ``EXEMPT_PATHS`` — nothing here opts in, the
    gate covers ``/api/`` by default and this route simply does not ask for an
    exception. Read through the settings singleton at request time, the way
    every other route does, so the suite's attribute patching is visible to it.
    """
    return VersionResponse(version=settings.version)
