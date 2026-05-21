"""Health check — 유일한 walking skeleton 구현 엔드포인트."""

from typing import Annotated

from fastapi import APIRouter, Depends

from reaction_backend.config import Settings, get_settings
from reaction_backend.schemas.common import HealthResponse

router = APIRouter(tags=["health"])

SettingsDep = Annotated[Settings, Depends(get_settings)]


@router.get("/health", response_model=HealthResponse)
async def health(settings: SettingsDep) -> HealthResponse:
    return HealthResponse(
        app=settings.app_name,
        version=settings.app_version,
        env=settings.app_env,
    )
