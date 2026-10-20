from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException
from selenium.common.exceptions import WebDriverException

from backend.services.social_browser_service import (
    complete_social_login,
    get_social_login_status,
    start_social_login,
)

router = APIRouter(prefix="/social-sessions")
SocialPlatform = Literal["instagram", "facebook"]


@router.get("/{platform}")
async def get_session(platform: SocialPlatform) -> dict:
    return get_social_login_status(platform)


@router.post("/{platform}/login")
async def open_login(platform: SocialPlatform) -> dict:
    try:
        return start_social_login(platform)
    except (OSError, RuntimeError, WebDriverException) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/{platform}/complete")
async def finish_login(platform: SocialPlatform) -> dict:
    try:
        return complete_social_login(platform)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except WebDriverException as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
