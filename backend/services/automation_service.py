from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator, model_validator

from backend.models.schedule import PlatformSchedule, ScheduleCreate
from backend.services.drive_service import (
    find_drive_video_by_name,
    upload_video_to_drive,
)
from backend.services.scheduler_service import (
    QUEUE_LOCK,
    _load_queue,
    create_schedule,
    normalize_schedule,
)

load_dotenv()

ROOT_DIR = Path(__file__).resolve().parents[2]
GENERATOR_OUTPUT_DIR = (
    ROOT_DIR / "video-generator-2.0" / "assets" / "output"
).resolve()
AUTOMATION_QUEUE_FILE = ROOT_DIR / "automation_queue.json"
AUTOMATION_SETTINGS_FILE = ROOT_DIR / "automation_settings.json"
AUTOMATION_LOCK = threading.RLock()
GENERATOR_API_URL = os.getenv(
    "VIDEO_GENERATOR_API_URL", "http://127.0.0.1:8001"
).rstrip("/")
logger = logging.getLogger(__name__)


class AutomationSettings(BaseModel):
    enabled: bool = True
    platforms: list[str] = Field(default_factory=lambda: ["youtube"], min_length=1)
    timezone: str = "Asia/Karachi"
    youtube_times: list[str] = Field(default_factory=lambda: ["17:30", "21:30"])
    telegram_times: list[str] = Field(default_factory=lambda: ["17:30", "21:30"])

    @field_validator("platforms")
    @classmethod
    def validate_platforms(cls, value: list[str]) -> list[str]:
        if not value or any(item not in {"youtube", "telegram"} for item in value):
            raise ValueError("Choose YouTube, Telegram, or both as automation destinations")
        return list(dict.fromkeys(value))

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        ZoneInfo(value)
        return value

    @field_validator("youtube_times", "telegram_times")
    @classmethod
    def validate_times(cls, value: list[str]) -> list[str]:
        for item in value:
            if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", item):
                raise ValueError("Publish times must use 24-hour HH:MM format")
        return value

    @model_validator(mode="after")
    def require_times_for_selected_platforms(self) -> AutomationSettings:
        if "youtube" in self.platforms and not self.youtube_times:
            raise ValueError("Add at least one YouTube publish time")
        if "telegram" in self.platforms and not self.telegram_times:
            raise ValueError("Add at least one Telegram publish time")
        return self


class PromptBatch(BaseModel):
    prompts: list[str] = Field(min_length=1, max_length=50)

    @field_validator("prompts")
    @classmethod
    def validate_prompts(cls, value: list[str]) -> list[str]:
        normalized = [prompt.strip() for prompt in value]
        if any(len(prompt) < 8 for prompt in normalized):
            raise ValueError("Each prompt must contain at least 8 characters")
        return normalized


def _load_json(path: Path, default: object) -> Any:
    with path.open("r", encoding="utf-8") as file:
        value = json.load(file)
    if not isinstance(value, type(default)):
        raise ValueError(f"{path.name} has an invalid JSON structure")
    return value


def _save_json(path: Path, value: object) -> None:
    temporary_path = path.with_suffix(f"{path.suffix}.tmp")
    with temporary_path.open("w", encoding="utf-8") as file:
        json.dump(value, file, indent=2)
        file.write("\n")
    os.replace(temporary_path, path)


def get_automation_settings() -> dict:
    with AUTOMATION_LOCK:
        if AUTOMATION_SETTINGS_FILE.exists():
            raw = _load_json(AUTOMATION_SETTINGS_FILE, {})
            return AutomationSettings.model_validate(raw).model_dump()
        return AutomationSettings(
            timezone=os.getenv("TIMEZONE", "Asia/Karachi"),
            youtube_times=[
                os.getenv("SLOT_A_TIME", "17:30"),
                os.getenv("SLOT_B_TIME", "21:30"),
            ],
            telegram_times=[
                os.getenv("SLOT_A_TIME", "17:30"),
                os.getenv("SLOT_B_TIME", "21:30"),
            ],
        ).model_dump()


def save_automation_settings(settings: AutomationSettings) -> dict:
    value = settings.model_dump()
    with AUTOMATION_LOCK:
        _save_json(AUTOMATION_SETTINGS_FILE, value)
    return value


def _load_jobs() -> list[dict]:
    if not AUTOMATION_QUEUE_FILE.exists():
        return []
    data = _load_json(AUTOMATION_QUEUE_FILE, [])
    if any(not isinstance(item, dict) for item in data):
        raise ValueError("automation_queue.json must contain an array of objects")
    return data


def _save_jobs(jobs: list[dict]) -> None:
    _save_json(AUTOMATION_QUEUE_FILE, jobs)


def list_automation_jobs() -> list[dict]:
    with AUTOMATION_LOCK:
        return list(reversed(_load_jobs()))


def enqueue_prompts(batch: PromptBatch) -> list[dict]:
    created_at = datetime.now().astimezone().isoformat()
    new_jobs = [
        {
            "job_id": str(uuid4()),
            "prompt": prompt,
            "status": "queued",
            "stage": "Waiting for an available worker",
            "progress_percent": 0,
            "created_at": created_at,
            "updated_at": created_at,
        }
        for prompt in batch.prompts
    ]
    with AUTOMATION_LOCK:
        jobs = _load_jobs()
        jobs.extend(new_jobs)
        _save_jobs(jobs)
    return new_jobs


def retry_automation_job(job_id: str) -> dict:
    with AUTOMATION_LOCK:
        jobs = _load_jobs()
        for job in jobs:
            if job.get("job_id") != job_id:
                continue
            if job.get("status") != "failed":
                raise ValueError("Only failed automation jobs can be retried")
            job["status"] = (
                "scheduling"
                if job.get("drive_file_id")
                else "rendering"
                if job.get("generation_id")
                else "queued"
            )
            job["stage"] = "Retry queued"
            job.pop("last_error", None)
            job["updated_at"] = datetime.now().astimezone().isoformat()
            _save_jobs(jobs)
            return dict(job)
    raise KeyError(f"Automation job {job_id} not found")


def _update_job(job_id: str, **changes: object) -> dict:
    with AUTOMATION_LOCK:
        jobs = _load_jobs()
        for job in jobs:
            if job.get("job_id") == job_id:
                job.update(changes)
                job["updated_at"] = datetime.now().astimezone().isoformat()
                _save_jobs(jobs)
                return dict(job)
    raise KeyError(f"Automation job {job_id} not found")


def _response_json(response: requests.Response, action: str) -> dict:
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if not response.ok:
        detail = payload.get("detail") or payload.get("message") or response.text
        raise RuntimeError(f"{action} failed ({response.status_code}): {detail}")
    if not isinstance(payload, dict):
        raise RuntimeError(f"{action} returned an invalid response")
    return payload


def _prepare_and_start_generation(job: dict) -> None:
    prepare_response = requests.post(
        f"{GENERATOR_API_URL}/api/automation/prepare",
        json={"prompt": job["prompt"]},
        timeout=600,
    )
    brief = _response_json(prepare_response, "AI prompt preparation")
    generation_payload = {
        key: brief[key]
        for key in (
            "prompt",
            "brand_name",
            "product",
            "objective",
            "target_audience",
            "tone",
            "language",
            "target_duration_seconds",
            "call_to_action",
            "website_text",
        )
        if key in brief
    }
    generation_response = requests.post(
        f"{GENERATOR_API_URL}/api/generations",
        json=generation_payload,
        timeout=30,
    )
    generation = _response_json(generation_response, "Video generation request")
    if not generation.get("id"):
        raise RuntimeError("Video generator did not return a generation ID")
    _update_job(
        job["job_id"],
        status="rendering",
        stage="Video generator is rendering the reviewed brief",
        progress_percent=int(generation.get("progress_percent") or 0),
        generation_id=generation["id"],
        generation_title=brief.get("title"),
        idea_provider=brief.get("idea_provider"),
        review_provider=brief.get("review_provider"),
        reviewed_prompt=brief.get("prompt"),
    )


def _slot_is_reserved(platform: str, candidate: datetime) -> bool:
    with QUEUE_LOCK:
        entries = _load_queue()
    for entry in entries:
        schedule = normalize_schedule(entry)
        if schedule["status"] not in {"scheduled", "processing", "failed"}:
            continue
        if platform not in schedule["platforms"]:
            continue
        target = schedule.get(platform) or {}
        if target.get("date") == candidate.date().isoformat() and target.get(
            "time"
        ) == candidate.strftime("%H:%M"):
            return True
    return False


def _next_platform_slot(platform: str, settings: AutomationSettings) -> PlatformSchedule:
    zone = ZoneInfo(settings.timezone)
    now = datetime.now(zone)
    times = (
        settings.youtube_times if platform == "youtube" else settings.telegram_times
    )
    candidates: list[datetime] = []
    for day_offset in range(31):
        day = (now + timedelta(days=day_offset)).date()
        for time_value in times:
            hour, minute = map(int, time_value.split(":"))
            candidate = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone)
            if candidate > now and not _slot_is_reserved(platform, candidate):
                candidates.append(candidate)
        if candidates:
            target = min(candidates)
            return PlatformSchedule(
                date=target.date().isoformat(),
                time=target.strftime("%H:%M"),
                status="scheduled",
            )
    raise RuntimeError(f"No available {platform} schedule slot found in the next 31 days")


def _schedule_drive_video(job: dict, settings: AutomationSettings) -> dict:
    existing_jobs = _load_queue()
    for item in existing_jobs:
        if (item.get("drive_file_id") or item.get("video_id")) == job["drive_file_id"]:
            return normalize_schedule(item)

    payload = ScheduleCreate(
        video_id=job["drive_file_id"],
        platforms=settings.platforms,
        youtube=(
            _next_platform_slot("youtube", settings)
            if "youtube" in settings.platforms
            else None
        ),
        telegram=(
            _next_platform_slot("telegram", settings)
            if "telegram" in settings.platforms
            else None
        ),
        timezone=settings.timezone,
    )
    return create_schedule(payload)


def _finish_generation(job: dict, settings: AutomationSettings) -> None:
    if not job.get("drive_file_id"):
        generation_response = requests.get(
            f"{GENERATOR_API_URL}/api/generations/{job['generation_id']}",
            timeout=30,
        )
        generation = _response_json(generation_response, "Video generation status")
        status = generation.get("status")
        if status in {"queued", "planning", "generating_script", "generating_voice",
                      "measuring_duration", "revising_script", "aligning_captions",
                      "selecting_assets", "rendering_scenes", "assembling_video",
                      "validating"}:
            _update_job(
                job["job_id"],
                status="rendering",
                stage=generation.get("current_stage") or "Rendering video",
                progress_percent=int(generation.get("progress_percent") or 0),
            )
            return
        if status != "completed":
            detail = generation.get("error_message") or generation.get("error_code") or status
            raise RuntimeError(f"Video generation ended with status {status}: {detail}")

        output_path = generation.get("output_path")
        if not output_path:
            raise RuntimeError("Completed generation did not provide an output path")
        video_path = Path(output_path).resolve()
        try:
            video_path.relative_to(GENERATOR_OUTPUT_DIR)
        except ValueError as exc:
            raise ValueError(
                "Video generator returned an output path outside its configured output folder"
            ) from exc
        if video_path.suffix.lower() != ".mp4" or not video_path.is_file():
            raise FileNotFoundError(f"Generated MP4 was not found: {video_path}")

        title = generation.get("title") or job.get("generation_title") or "video"
        slug = re.sub(r"[^A-Za-z0-9_-]+", "-", title).strip("-_")[:60] or "video"
        zone = ZoneInfo(settings.timezone)
        timestamp = datetime.now(zone).strftime("%Y%m%d-%H%M")
        filename = job.get("generated_filename") or (
            f"{timestamp}-{slug}-{job['job_id'][:8]}.mp4"
        )
        _update_job(
            job["job_id"],
            status="uploading",
            stage="Saving generated video to Google Drive",
            progress_percent=95,
            generated_filename=filename,
        )
        drive_video = find_drive_video_by_name(filename)
        if drive_video is None:
            drive_video = upload_video_to_drive(video_path, filename)
        job = _update_job(
            job["job_id"],
            status="scheduling",
            stage="Choosing the next available publishing slot",
            progress_percent=98,
            drive_file_id=drive_video["id"],
            drive_filename=drive_video["name"],
        )

    schedule = _schedule_drive_video(job, settings)
    _update_job(
        job["job_id"],
        status="scheduled",
        stage="Saved to Drive and added to the publishing queue",
        progress_percent=100,
        schedule_id=schedule["schedule_id"],
        drive_url=f"https://drive.google.com/file/d/{job['drive_file_id']}/view",
    )


def process_automation_queue() -> None:
    settings = AutomationSettings.model_validate(get_automation_settings())
    if not settings.enabled:
        return
    with AUTOMATION_LOCK:
        job = next(
            (
                item
                for item in _load_jobs()
                if item.get("status")
                in {"queued", "preparing", "rendering", "uploading", "scheduling"}
            ),
            None,
        )
    if job is None:
        return
    try:
        if job["status"] in {"queued", "preparing"}:
            _update_job(
                job["job_id"],
                status="preparing",
                stage="Idea model is expanding the prompt; reviewer model checks it next",
            )
            _prepare_and_start_generation(job)
        elif job["status"] in {"rendering", "uploading", "scheduling"}:
            _finish_generation(job, settings)
    except Exception as exc:
        logger.exception("Automation job failed job_id=%s", job["job_id"])
        _update_job(
            job["job_id"],
            status="failed",
            stage="Automation failed",
            last_error=f"{type(exc).__name__}: {exc}",
        )
