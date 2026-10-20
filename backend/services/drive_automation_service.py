from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator, model_validator

from backend.models.schedule import PlatformSchedule, ScheduleCreate, is_facebook_page_url
from backend.services.drive_service import list_drive_videos
from backend.services.scheduler_service import (
    QUEUE_LOCK,
    _load_queue,
    create_schedule,
    normalize_schedule,
    reserved_video_ids,
)

ROOT_DIR = Path(__file__).resolve().parents[2]
DRIVE_AUTOMATION_SETTINGS_FILE = ROOT_DIR / "drive_automation_settings.json"
DRIVE_AUTOMATION_STATE_FILE = ROOT_DIR / "drive_automation_state.json"
DRIVE_AUTOMATION_LOCK = threading.RLock()
_PROCESS_LOCK = threading.Lock()
logger = logging.getLogger(__name__)


class DriveAutomationSettings(BaseModel):
    enabled: bool = False
    interval_minutes: int = Field(default=30, ge=1, le=1440)
    max_videos_per_day: int = Field(default=3, ge=1, le=50)
    platforms: list[Literal["youtube", "instagram", "facebook"]] = Field(
        default_factory=lambda: ["youtube"],
        min_length=1,
    )
    timezone: str = Field(default_factory=lambda: os.getenv("TIMEZONE", "Asia/Karachi"))
    facebook_page_url: str = Field(
        default_factory=lambda: os.getenv("FACEBOOK_PAGE_URL", "").strip()
    )

    @field_validator("platforms")
    @classmethod
    def validate_platforms(
        cls,
        value: list[Literal["youtube", "instagram", "facebook"]],
    ) -> list[Literal["youtube", "instagram", "facebook"]]:
        if not value:
            raise ValueError("Select at least one publishing platform")
        return list(dict.fromkeys(value))

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        ZoneInfo(value)
        return value

    @model_validator(mode="after")
    def require_facebook_page(self) -> DriveAutomationSettings:
        if "facebook" in self.platforms and not is_facebook_page_url(
            self.facebook_page_url
        ):
            raise ValueError(
                "Enter the full HTTPS Facebook Page URL copied from the Page you manage"
            )
        return self


def _read_json(path: Path, default: object) -> object:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as file:
        value = json.load(file)
    if not isinstance(value, type(default)):
        raise ValueError(f"{path.name} has an invalid JSON structure")
    return value


def _write_json(path: Path, value: object) -> None:
    temporary_path = path.with_suffix(f"{path.suffix}.tmp")
    with temporary_path.open("w", encoding="utf-8") as file:
        json.dump(value, file, indent=2)
        file.write("\n")
    os.replace(temporary_path, path)


def _load_settings() -> DriveAutomationSettings:
    with DRIVE_AUTOMATION_LOCK:
        raw = _read_json(DRIVE_AUTOMATION_SETTINGS_FILE, {})
        return DriveAutomationSettings.model_validate(raw)


def get_drive_automation_settings() -> dict:
    return _load_settings().model_dump()


def save_drive_automation_settings(
    settings: DriveAutomationSettings,
) -> dict:
    required_social_platforms = set(settings.platforms) & {"instagram", "facebook"}
    if settings.enabled and required_social_platforms:
        from backend.services.social_browser_service import get_social_login_status

        unauthenticated = [
            platform
            for platform in sorted(required_social_platforms)
            if not get_social_login_status(platform)["authenticated"]
        ]
        if unauthenticated:
            raise RuntimeError(
                "Sign in to these platforms in Automation first: "
                + ", ".join(unauthenticated)
            )
    value = settings.model_dump()
    with DRIVE_AUTOMATION_LOCK:
        previous = DriveAutomationSettings.model_validate(
            _read_json(DRIVE_AUTOMATION_SETTINGS_FILE, {})
        )
        state = _read_json(
            DRIVE_AUTOMATION_STATE_FILE,
            {"enabled_since": None, "scheduled_video_ids": []},
        )
        if not isinstance(state, dict):
            raise ValueError("drive_automation_state.json must contain an object")
        if settings.enabled and (
            not previous.enabled or not state.get("enabled_since")
        ):
            state["enabled_since"] = datetime.now(timezone.utc).isoformat()
        elif not settings.enabled:
            state["enabled_since"] = None
        _write_json(DRIVE_AUTOMATION_SETTINGS_FILE, value)
        _write_json(DRIVE_AUTOMATION_STATE_FILE, state)
    return value


def _load_state() -> dict:
    with DRIVE_AUTOMATION_LOCK:
        state = _read_json(
            DRIVE_AUTOMATION_STATE_FILE,
            {"enabled_since": None, "scheduled_video_ids": []},
        )
    if not isinstance(state, dict):
        raise ValueError("drive_automation_state.json must contain an object")
    if not isinstance(state.get("scheduled_video_ids", []), list):
        raise ValueError("scheduled_video_ids must be an array")
    return state


def _save_scheduled_id(video_id: str) -> None:
    with DRIVE_AUTOMATION_LOCK:
        state = _load_state()
        scheduled_ids = set(state.get("scheduled_video_ids", []))
        scheduled_ids.add(video_id)
        state["scheduled_video_ids"] = sorted(scheduled_ids)
        _write_json(DRIVE_AUTOMATION_STATE_FILE, state)


def _last_platform_schedule(
    platform: str,
    settings: DriveAutomationSettings,
) -> datetime | None:
    latest: datetime | None = None
    with QUEUE_LOCK:
        queue = _load_queue()
    for entry in queue:
        normalized = normalize_schedule(entry)
        if platform not in normalized["platforms"]:
            continue
        platform_schedule = normalized[platform]
        if platform_schedule.get("status") not in {
            "scheduled",
            "processing",
            "failed",
            "published",
            "sent",
        }:
            continue
        if not platform_schedule.get("date") or not platform_schedule.get("time"):
            continue
        zone = ZoneInfo(normalized.get("timezone") or settings.timezone)
        scheduled_at = datetime.fromisoformat(
            f"{platform_schedule['date']}T{platform_schedule['time']}"
        ).replace(tzinfo=zone)
        if latest is None or scheduled_at > latest:
            latest = scheduled_at
    return latest


def _scheduled_platform_count_for_day(
    platform: str,
    local_date: str,
    settings: DriveAutomationSettings,
) -> int:
    count = 0
    with QUEUE_LOCK:
        queue = _load_queue()
    for entry in queue:
        if entry.get("status") == "cancelled":
            continue
        normalized = normalize_schedule(entry)
        if platform not in normalized["platforms"]:
            continue
        platform_schedule = normalized[platform]
        if not platform_schedule.get("date") or not platform_schedule.get("time"):
            continue
        if platform_schedule.get("status") in {"cancelled", "not_selected"}:
            continue
        schedule_zone = ZoneInfo(normalized.get("timezone") or settings.timezone)
        local_schedule_at = datetime.fromisoformat(
            f"{platform_schedule['date']}T{platform_schedule['time']}"
        ).replace(tzinfo=schedule_zone)
        if local_schedule_at.astimezone(ZoneInfo(settings.timezone)).date().isoformat() == local_date:
            count += 1
    return count


def _next_platform_schedule(
    platform: str,
    settings: DriveAutomationSettings,
    now: datetime | None = None,
) -> PlatformSchedule:
    zone = ZoneInfo(settings.timezone)
    current = now.astimezone(zone) if now else datetime.now(zone)
    if current.second or current.microsecond:
        current = (current + timedelta(minutes=1)).replace(second=0, microsecond=0)
    scheduled_at = current + timedelta(minutes=settings.interval_minutes)
    previous = _last_platform_schedule(platform, settings)
    if previous:
        previous = previous.astimezone(zone)
        scheduled_at = max(
            scheduled_at,
            previous + timedelta(minutes=settings.interval_minutes),
        )
    slot_time = scheduled_at.time()
    while (
        _scheduled_platform_count_for_day(
            platform,
            scheduled_at.date().isoformat(),
            settings,
        )
        >= settings.max_videos_per_day
    ):
        scheduled_at = datetime.combine(
            scheduled_at.date() + timedelta(days=1),
            slot_time,
            tzinfo=zone,
        )
        if previous:
            scheduled_at = max(
                scheduled_at,
                previous + timedelta(minutes=settings.interval_minutes),
            )
        slot_time = scheduled_at.time()
    return PlatformSchedule(
        date=scheduled_at.date().isoformat(),
        time=scheduled_at.strftime("%H:%M"),
        status="scheduled",
    )


def process_new_drive_videos(now: datetime | None = None) -> int:
    settings = _load_settings()
    if not settings.enabled:
        return 0

    with _PROCESS_LOCK:
        reserved_ids = reserved_video_ids()
        videos = list_drive_videos()
        scheduled_count = 0

        for video in sorted(videos, key=lambda item: item.get("created_at") or ""):
            video_id = str(video.get("drive_file_id") or video.get("id") or "")
            if not video_id:
                continue
            if video_id in reserved_ids:
                _save_scheduled_id(video_id)
                continue

            payload = ScheduleCreate(
                video_id=video_id,
                platforms=settings.platforms,
                **{
                    platform: _next_platform_schedule(platform, settings, now)
                    for platform in settings.platforms
                },
                facebook_page_url=(
                    settings.facebook_page_url
                    if "facebook" in settings.platforms
                    else None
                ),
                timezone=settings.timezone,
            )
            create_schedule(payload)
            _save_scheduled_id(video_id)
            reserved_ids.add(video_id)
            scheduled_count += 1

        return scheduled_count
