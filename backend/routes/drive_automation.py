from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException

from backend.services.drive_automation_service import (
    DriveAutomationSettings,
    get_drive_automation_settings,
    process_new_drive_videos,
    save_drive_automation_settings,
)

router = APIRouter(prefix="/drive-automation")
logger = logging.getLogger(__name__)


@router.get("/settings")
async def get_settings() -> dict:
    return get_drive_automation_settings()


@router.put("/settings")
async def put_settings(payload: DriveAutomationSettings) -> dict:
    try:
        settings = save_drive_automation_settings(payload)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    scheduled_count = 0
    scheduling_error = None
    if payload.enabled:
        try:
            scheduled_count = await asyncio.to_thread(process_new_drive_videos)
        except Exception as exc:
            logger.exception("Drive scheduling check failed after saving settings")
            scheduling_error = f"{type(exc).__name__}: {exc}"

    return {
        "settings": settings,
        "scheduled_count": scheduled_count,
        "scheduling_error": scheduling_error,
    }
