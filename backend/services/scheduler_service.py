from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from backend.models.schedule import ScheduleCreate, ScheduleUpdate
from backend.services.drive_service import get_drive_video

ROOT_DIR = Path(__file__).resolve().parents[2]
QUEUE_FILE = ROOT_DIR / "publish_queue.json"
PROCESSED_LOG_FILE = ROOT_DIR / "processed_log.json"
QUEUE_LOCK = threading.RLock()
logger = logging.getLogger(__name__)


def _load_queue() -> list[dict]:
    with QUEUE_LOCK:
        if not QUEUE_FILE.exists():
            return []
        with QUEUE_FILE.open("r", encoding="utf-8") as file:
            data = json.load(file)
        if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
            raise ValueError("publish_queue.json must contain a JSON array of objects")

        migrated = False
        for item in data:
            if (
                not item.get("schedule_id")
                and not item.get("id")
                and any(
                    item.get(key)
                    for key in (
                        "video_id",
                        "drive_file_id",
                        "youtube_video_id",
                        "file_name",
                        "filename",
                    )
                )
            ):
                item["schedule_id"] = str(uuid4())
                migrated = True
            if item.get("schedule_id") and item.get("video_id") and not item.get("drive_file_id"):
                item["drive_file_id"] = item["video_id"]
                migrated = True
        if migrated:
            _save_queue(data)
        return data


def _save_queue(queue: list[dict]) -> None:
    with QUEUE_LOCK:
        temp_path = QUEUE_FILE.with_suffix(f"{QUEUE_FILE.suffix}.tmp")
        with temp_path.open("w", encoding="utf-8") as file:
            json.dump(queue, file, indent=2)
            file.write("\n")
        os.replace(temp_path, QUEUE_FILE)


def _normalize_platform(value: object) -> dict:
    if isinstance(value, dict):
        return {
            "date": value.get("date"),
            "time": value.get("time"),
            "status": value.get("status") or "scheduled",
        }
    return {"date": None, "time": None, "status": "not_selected"}


def normalize_schedule(entry: dict) -> dict:
    schedule_id = entry.get("schedule_id") or entry.get("id") or str(uuid4())
    platforms = entry.get("platforms") or entry.get("destinations") or []
    if not isinstance(platforms, list):
        platforms = [platforms] if platforms else []
    if not platforms:
        platforms = [
            platform
            for platform in ("youtube", "telegram", "instagram", "facebook")
            if (
                entry.get("youtube_video_id")
                if platform == "youtube"
                else entry.get(f"{platform}_status")
            )
            not in (None, "not_selected", False)
        ]
    platform_schedules = {
        platform: _normalize_platform(entry.get(platform))
        for platform in ("youtube", "telegram", "instagram", "facebook")
    }
    youtube = platform_schedules["youtube"]
    if entry.get("go_live_at") and not youtube.get("date"):
        try:
            go_live_at = datetime.fromisoformat(entry["go_live_at"])
            youtube["date"] = go_live_at.date().isoformat()
            youtube["time"] = go_live_at.strftime("%H:%M")
        except (TypeError, ValueError):
            logger.warning("Invalid legacy go_live_at value in schedule %s", schedule_id)
    if entry.get("youtube_status"):
        youtube["status"] = entry["youtube_status"]
    if "youtube" in platforms and youtube.get("status") == "not_selected":
        youtube["status"] = "scheduled"
    if "youtube" not in platforms and entry.get("youtube_video_id"):
        youtube = {
            "date": entry.get("youtube_date"),
            "time": entry.get("youtube_time"),
            "status": entry.get("youtube_status") or "scheduled",
        }
    for platform in ("telegram", "instagram", "facebook"):
        schedule = platform_schedules[platform]
        legacy_status = entry.get(f"{platform}_status")
        if legacy_status:
            schedule["status"] = legacy_status
        if platform in platforms and schedule.get("status") == "not_selected":
            schedule["status"] = "scheduled"
        if platform not in platforms and legacy_status:
            schedule.update(
                {
                    "date": entry.get(f"{platform}_date"),
                    "time": entry.get(f"{platform}_time"),
                    "status": legacy_status,
                }
            )
    status = entry.get("status")
    if entry.get("published") and all(
        entry.get(f"{platform}_status") in (None, "sent", "published", "not_selected")
        for platform in platforms
    ):
        status = "completed"
    return {
        "schedule_id": schedule_id,
        "video_id": entry.get("video_id") or entry.get("drive_file_id") or entry.get("youtube_video_id") or entry.get("file_name") or schedule_id,
        "filename": entry.get("filename") or entry.get("file_name") or entry.get("name") or "video.mp4",
        "thumbnail": entry.get("thumbnail") or "",
        "platforms": platforms,
        "facebook_page_url": entry.get("facebook_page_url"),
        "youtube": youtube,
        **platform_schedules,
        "timezone": entry.get("timezone") or os.getenv("TIMEZONE", "Asia/Karachi"),
        "status": status or ("scheduled" if platforms else "unknown"),
        "last_error": entry.get("last_error"),
        "created_at": entry.get("created_at") or datetime.now(timezone.utc).isoformat(),
        "updated_at": entry.get("updated_at") or entry.get("created_at") or datetime.now(timezone.utc).isoformat(),
    }


def list_schedules() -> list[dict]:
    queue = _load_queue()
    schedules = []
    for item in queue:
        normalized = normalize_schedule(item)
        has_explicit_times = all(
            normalized[platform]["date"] and normalized[platform]["time"]
            for platform in normalized["platforms"]
        )
        if normalized["status"] in {"scheduled", "processing", "failed"} and has_explicit_times:
            schedules.append(normalized)
    return schedules


def create_schedule(payload: ScheduleCreate) -> dict:
    queue = _load_queue()
    reserved = {
        item.get("video_id") or item.get("drive_file_id")
        for item in queue
        if item.get("status") not in {"cancelled", "completed"}
    }
    if payload.video_id in reserved:
        raise ValueError("This video already has a schedule")
    if payload.video_id in _processed_video_ids():
        raise ValueError("This video has already been processed")
    platform_payloads = {
        platform: getattr(payload, platform)
        for platform in ("youtube", "telegram", "instagram", "facebook")
    }
    _validate_future_schedules(
        payload.platforms,
        {
            platform: schedule.model_dump() if schedule else None
            for platform, schedule in platform_payloads.items()
        },
        payload.timezone,
    )
    created_at = datetime.now(timezone.utc).isoformat()
    video_data = get_drive_video(payload.video_id)
    if video_data is None:
        raise ValueError("The selected video is no longer available in Google Drive")
    schedule = {
        "schedule_id": str(uuid4()),
        "video_id": payload.video_id,
        "drive_file_id": payload.video_id,
        "file_name": video_data["filename"],
        "filename": video_data["filename"] if video_data else payload.video_id,
        "thumbnail": video_data["thumbnail"] if video_data else "",
        "platforms": payload.platforms,
        "facebook_page_url": payload.facebook_page_url,
        **{
            platform: {
                "date": schedule.date if schedule else None,
                "time": schedule.time if schedule else None,
                "status": schedule.status if schedule else "not_selected",
            }
            for platform, schedule in platform_payloads.items()
        },
        "timezone": payload.timezone,
        "status": "scheduled",
        "created_at": created_at,
        "updated_at": created_at,
    }
    queue.append(schedule)
    _save_queue(queue)
    return normalize_schedule(schedule)


def update_schedule(schedule_id: str, payload: ScheduleUpdate) -> dict:
    with QUEUE_LOCK:
        queue = _load_queue()
        for index, item in enumerate(queue):
            if str(item.get("schedule_id") or item.get("id")) == schedule_id:
                normalized = normalize_schedule(item)
                if normalized["status"] not in {"scheduled", "failed"}:
                    raise ValueError("Only pending or failed jobs can be edited")
                updated = dict(item)
                if payload.video_id is not None:
                    already_reserved = {
                        value
                        for value in reserved_video_ids()
                        if value != (item.get("video_id") or item.get("drive_file_id"))
                    }
                    if payload.video_id in already_reserved:
                        raise ValueError("This video already has a schedule or was processed")
                    video_data = get_drive_video(payload.video_id)
                    if video_data is None:
                        raise ValueError("The selected video is no longer available in Google Drive")
                    updated["video_id"] = payload.video_id
                    updated["drive_file_id"] = payload.video_id
                    updated["file_name"] = video_data["filename"]
                    updated["filename"] = video_data["filename"]
                    updated["thumbnail"] = video_data["thumbnail"]
                if payload.platforms is not None:
                    updated["platforms"] = payload.platforms
                if payload.timezone is not None:
                    updated["timezone"] = payload.timezone
                if payload.facebook_page_url is not None:
                    updated["facebook_page_url"] = payload.facebook_page_url
                for platform in ("youtube", "telegram", "instagram", "facebook"):
                    schedule_payload = getattr(payload, platform)
                    if schedule_payload is not None:
                        updated[platform] = schedule_payload.model_dump()
                platforms = updated.get("platforms", normalized["platforms"])
                timezone_name = updated.get("timezone", normalized["timezone"])
                _validate_future_schedules(
                    platforms,
                    {
                        platform: updated.get(platform, normalized[platform])
                        for platform in ("youtube", "telegram", "instagram", "facebook")
                    },
                    timezone_name,
                )
                for platform in platforms:
                    if updated.get(f"{platform}_status") == "failed":
                        updated[f"{platform}_status"] = "scheduled"
                    if updated.get(platform, {}).get("status") == "failed":
                        updated[platform]["status"] = "scheduled"
                updated.pop("last_error", None)
                updated["status"] = "scheduled"
                updated["updated_at"] = datetime.now(timezone.utc).isoformat()
                queue[index] = updated
                _save_queue(queue)
                return normalize_schedule(updated)
    raise KeyError(f"Schedule {schedule_id} not found")


def delete_schedule(schedule_id: str) -> None:
    queue = _load_queue()
    matching = next(
        (item for item in queue if str(item.get("schedule_id") or item.get("id")) == schedule_id),
        None,
    )
    if matching is not None:
        normalized = normalize_schedule(matching)
        current_status = normalized["status"]
        delivered = any(
            normalized[platform].get("status")
            in {"published", "sent"}
            for platform in normalized["platforms"]
        )
        platform_statuses = [
            normalized[platform].get("status", "scheduled")
            for platform in normalized["platforms"]
        ]
        if "processing" in platform_statuses:
            raise ValueError("A job cannot be removed while a delivery is processing")
        has_failed_delivery = "failed" in platform_statuses
        if current_status not in {"scheduled", "failed"} or (
            delivered and not has_failed_delivery
        ):
            raise ValueError("A job cannot be removed after delivery has completed")
        remaining = [
            item
            for item in queue
            if str(item.get("schedule_id") or item.get("id")) != schedule_id
        ]
    else:
        remaining = queue
    if len(remaining) == len(queue):
        raise KeyError(f"Schedule {schedule_id} not found")
    _save_queue(remaining)


def retry_schedule(schedule_id: str) -> dict:
    queue = _load_queue()
    for index, item in enumerate(queue):
        if str(item.get("schedule_id") or item.get("id")) == schedule_id:
            failed_platforms = [
                platform
                for platform in item.get("platforms", [])
                if item.get(f"{platform}_status") == "failed"
                or item.get(platform, {}).get("status") == "failed"
            ]
            if not failed_platforms:
                raise ValueError("This schedule has no failed platform to retry")
            item["status"] = "scheduled"
            item["updated_at"] = datetime.now(timezone.utc).isoformat()
            item.pop("last_error", None)
            for platform in failed_platforms:
                item.setdefault(platform, {})["status"] = "scheduled"
                item[f"{platform}_status"] = "scheduled"
            queue[index] = item
            _save_queue(queue)
            return normalize_schedule(item)
    raise KeyError(f"Schedule {schedule_id} not found")


def _processed_video_ids() -> set[str]:
    if not PROCESSED_LOG_FILE.exists():
        return set()
    with PROCESSED_LOG_FILE.open("r", encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, dict):
        raise ValueError("processed_log.json must contain a JSON object")
    ids = data.get("processed_file_ids", [])
    return {str(value) for value in ids}


def reserved_video_ids() -> set[str]:
    reserved = _processed_video_ids()
    for item in _load_queue():
        video_id = item.get("video_id") or item.get("drive_file_id")
        if video_id and item.get("status") not in {"cancelled"}:
            reserved.add(str(video_id))
    return reserved


def _validate_future_schedules(
    platforms: list[str],
    schedules: dict[str, dict | object | None],
    timezone_name: str,
) -> None:
    zone = ZoneInfo(timezone_name)
    now = datetime.now(zone)
    for platform in platforms:
        schedule = schedules.get(platform)
        if hasattr(schedule, "model_dump"):
            schedule = schedule.model_dump()
        if not isinstance(schedule, dict) or not schedule.get("date") or not schedule.get("time"):
            raise ValueError(f"{platform} requires both a date and a time")
        scheduled_at = datetime.fromisoformat(
            f"{schedule['date']}T{schedule['time']}"
        ).replace(tzinfo=zone)
        if scheduled_at <= now:
            raise ValueError(f"{platform} schedule must be in the future")
