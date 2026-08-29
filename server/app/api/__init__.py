from fastapi import APIRouter

from . import (
    accounts,
    assets,
    events,
    jobs,
    presets,
    projects,
    publications,
    segments,
    system,
)

api_router = APIRouter()
for module in (
    projects,
    segments,
    presets,
    assets,
    accounts,
    publications,
    jobs,
    events,
    system,
):
    api_router.include_router(module.router)

__all__ = ["api_router"]
