from __future__ import annotations

from fastapi import APIRouter, HTTPException
from googleapiclient.discovery import build

from auth import get_credentials

router = APIRouter(prefix="/youtube")


@router.get("/channel")
async def get_channel() -> dict:
    try:
        youtube = build("youtube", "v3", credentials=get_credentials())
        result = youtube.channels().list(part="snippet", mine=True).execute()
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail=f"YouTube channel is unavailable: {exc}"
        ) from exc
    channels = result.get("items", [])
    if not channels:
        raise HTTPException(status_code=404, detail="No YouTube channel is linked")
    snippet = channels[0].get("snippet", {})
    thumbnails = snippet.get("thumbnails", {})
    image = (
        thumbnails.get("high")
        or thumbnails.get("medium")
        or thumbnails.get("default")
        or {}
    )
    return {
        "title": snippet.get("title", "YouTube channel"),
        "thumbnail": image.get("url"),
    }
