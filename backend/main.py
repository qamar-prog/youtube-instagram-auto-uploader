from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.routes.ai import router as ai_router
from backend.routes.automation import router as automation_router
from backend.routes.drive_automation import router as drive_automation_router
from backend.routes.schedules import router as schedules_router
from backend.routes.social_sessions import router as social_sessions_router
from backend.routes.videos import router as videos_router
from backend.routes.youtube import router as youtube_router
from backend.services.automation_service import process_automation_queue
from backend.services.drive_automation_service import process_new_drive_videos
from backend.services.publishing_worker import process_due_schedules
from backend.services.social_browser_service import close_social_browsers

logger = logging.getLogger(__name__)
scheduler_state = {
    "running": False,
    "last_checked_at": None,
    "last_error": None,
}
automation_state = {"running": False, "last_error": None}
drive_automation_state = {
    "running": False,
    "last_checked_at": None,
    "last_error": None,
}


async def _schedule_worker() -> None:
    interval = max(5, int(os.getenv("SCHEDULER_POLL_SECONDS", "15")))
    scheduler_state["running"] = True
    try:
        while True:
            try:
                await asyncio.to_thread(process_due_schedules)
                scheduler_state["last_error"] = None
            except Exception as exc:
                scheduler_state["last_error"] = f"{type(exc).__name__}: {exc}"
                logger.exception("Scheduled publishing check failed")
            finally:
                scheduler_state["last_checked_at"] = datetime.now(
                    timezone.utc
                ).isoformat()
            await asyncio.sleep(interval)
    finally:
        scheduler_state["running"] = False


async def _automation_worker() -> None:
    interval = max(5, int(os.getenv("AUTOMATION_POLL_SECONDS", "15")))
    automation_state["running"] = True
    try:
        while True:
            try:
                await asyncio.to_thread(process_automation_queue)
                automation_state["last_error"] = None
            except Exception as exc:
                automation_state["last_error"] = f"{type(exc).__name__}: {exc}"
                logger.exception("Automated video generation check failed")
            await asyncio.sleep(interval)
    finally:
        automation_state["running"] = False


async def _drive_automation_worker() -> None:
    interval = max(15, int(os.getenv("DRIVE_AUTOMATION_POLL_SECONDS", "60")))
    drive_automation_state["running"] = True
    try:
        while True:
            try:
                await asyncio.to_thread(process_new_drive_videos)
                drive_automation_state["last_error"] = None
            except Exception as exc:
                drive_automation_state["last_error"] = f"{type(exc).__name__}: {exc}"
                logger.exception("Drive auto-publishing check failed")
            finally:
                drive_automation_state["last_checked_at"] = datetime.now(
                    timezone.utc
                ).isoformat()
            await asyncio.sleep(interval)
    finally:
        drive_automation_state["running"] = False


@asynccontextmanager
async def lifespan(_app: FastAPI):
    workers = [
        asyncio.create_task(_schedule_worker()),
        asyncio.create_task(_automation_worker()),
        asyncio.create_task(_drive_automation_worker()),
    ]
    try:
        yield
    finally:
        for worker in workers:
            worker.cancel()
        for worker in workers:
            try:
                await worker
            except asyncio.CancelledError:
                pass
        await asyncio.to_thread(close_social_browsers)


app = FastAPI(title="Video Automation Dashboard API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def root() -> dict:
    return {
        "message": "Video Automation Dashboard API",
        "docs": "/docs",
        "health": "/api/health",
    }


@app.get("/api/health")
async def health() -> dict:
    return {
        "status": "ok",
        "scheduler": scheduler_state,
        "automation": automation_state,
        "drive_automation": drive_automation_state,
    }


app.include_router(videos_router, prefix="/api")
app.include_router(schedules_router, prefix="/api")
app.include_router(social_sessions_router, prefix="/api")
app.include_router(ai_router, prefix="/api")
app.include_router(automation_router, prefix="/api")
app.include_router(drive_automation_router, prefix="/api")
app.include_router(youtube_router, prefix="/api")

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend.main:app", host="0.0.0.0", port=8000, reload=True)
