from __future__ import annotations

from fastapi import APIRouter, HTTPException

from backend.services.drive_service import list_drive_videos
from backend.services.scheduler_service import reserved_video_ids

router = APIRouter(prefix="/videos")


@router.get("")
async def get_videos() -> list[dict]:
    try:
        reserved = reserved_video_ids()
        return [
            video
            for video in list_drive_videos()
            if video["drive_file_id"] not in reserved
        ]
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover - defensive endpoint safety
        raise HTTPException(status_code=503, detail=f"Drive service unavailable: {exc}") from exc
