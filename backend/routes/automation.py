from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from backend.services.automation_service import (
    AutomationSettings,
    PromptBatch,
    enqueue_prompts,
    get_automation_settings,
    list_automation_jobs,
    retry_automation_job,
    save_automation_settings,
)

router = APIRouter(prefix="/automation")


@router.get("")
async def get_automation() -> dict:
    return {
        "settings": get_automation_settings(),
        "jobs": list_automation_jobs(),
    }


@router.put("/settings")
async def put_automation_settings(payload: AutomationSettings) -> dict:
    try:
        return save_automation_settings(payload)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/prompts", status_code=status.HTTP_201_CREATED)
async def post_automation_prompts(payload: PromptBatch) -> list[dict]:
    try:
        return enqueue_prompts(payload)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/jobs/{job_id}/retry")
async def post_automation_retry(job_id: str) -> dict:
    try:
        return retry_automation_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
