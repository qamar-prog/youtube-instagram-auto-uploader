from __future__ import annotations

from pydantic import BaseModel, Field
from fastapi import APIRouter

from backend.services.groq_service import generate_ai_metadata

router = APIRouter(prefix="/ai")


class MetadataRequest(BaseModel):
    filename: str = Field(..., min_length=1)
    video_context: str = ""


@router.post("/generate-metadata")
async def generate_metadata(payload: MetadataRequest) -> dict:
    return generate_ai_metadata(payload.filename, payload.video_context)
