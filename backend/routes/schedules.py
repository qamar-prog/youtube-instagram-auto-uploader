from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from backend.models.schedule import ScheduleCreate, ScheduleUpdate
from backend.services.scheduler_service import create_schedule, delete_schedule, list_schedules, retry_schedule, update_schedule

router = APIRouter(prefix="/schedules")


@router.get("")
async def get_schedules() -> list[dict]:
    return list_schedules()


@router.post("", status_code=status.HTTP_201_CREATED)
async def post_schedule(payload: ScheduleCreate):
    try:
        return create_schedule(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/{schedule_id}")
async def put_schedule(schedule_id: str, payload: ScheduleUpdate):
    try:
        return update_schedule(schedule_id, payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.delete("/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_schedule_endpoint(schedule_id: str):
    try:
        delete_schedule(schedule_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return None


@router.post("/{schedule_id}/retry")
async def retry_schedule_endpoint(schedule_id: str):
    try:
        return retry_schedule(schedule_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
