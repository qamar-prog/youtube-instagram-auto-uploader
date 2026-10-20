from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from backend.services.scheduler_service import (
    QUEUE_LOCK,
    _load_queue,
    _save_queue,
    normalize_schedule,
)
from utils import load_json, save_json

logger = logging.getLogger(__name__)
PROCESSING_TIMEOUT = timedelta(minutes=30)
_METADATA_LOCK = threading.RLock()


def _scheduled_at(entry: dict, platform: str) -> datetime | None:
    schedule = entry.get(platform)
    timezone_name = entry.get("timezone") or os.getenv("TIMEZONE", "Asia/Karachi")
    if isinstance(schedule, dict) and schedule.get("date") and schedule.get("time"):
        return datetime.fromisoformat(
            f"{schedule['date']}T{schedule['time']}"
        ).replace(tzinfo=ZoneInfo(timezone_name))
    if platform == "youtube" and entry.get("go_live_at"):
        value = datetime.fromisoformat(entry["go_live_at"])
        if value.tzinfo is None:
            value = value.replace(tzinfo=ZoneInfo(timezone_name))
        return value
    return None


def _platform_status(entry: dict, platform: str) -> str:
    platform_data = entry.get(platform)
    if isinstance(platform_data, dict) and platform_data.get("status"):
        return str(platform_data["status"])
    return str(entry.get(f"{platform}_status", "scheduled"))


def _change_job(schedule_id: str, changes: dict) -> dict | None:
    with QUEUE_LOCK:
        queue = _load_queue()
        for index, entry in enumerate(queue):
            if str(entry.get("schedule_id") or entry.get("id")) == schedule_id:
                entry.update(changes)
                entry["updated_at"] = datetime.now().astimezone().isoformat()
                queue[index] = entry
                _save_queue(queue)
                return dict(entry)
    return None


def _set_platform_status(
    schedule_id: str,
    platform: str,
    status: str,
    extra: dict | None = None,
) -> dict | None:
    with QUEUE_LOCK:
        queue = _load_queue()
        for index, entry in enumerate(queue):
            if str(entry.get("schedule_id") or entry.get("id")) != schedule_id:
                continue
            entry.setdefault(platform, {})["status"] = status
            entry[f"{platform}_status"] = status
            if extra:
                entry.update(extra)
            entry["updated_at"] = datetime.now().astimezone().isoformat()
            selected = entry.get("platforms") or entry.get("destinations") or []
            delivered_statuses = {
                "youtube": {"published"},
                "telegram": {"sent"},
                "instagram": {"published"},
                "facebook": {"published"},
            }
            if selected and all(
                _platform_status(entry, name)
                in delivered_statuses[name]
                for name in selected
            ):
                entry["status"] = "completed"
                entry["published"] = True
                _record_processed_video(entry.get("drive_file_id") or entry.get("video_id"))
            elif any(_platform_status(entry, name) == "failed" for name in selected):
                entry["status"] = "failed"
            else:
                entry["status"] = "scheduled"
            queue[index] = entry
            _save_queue(queue)
            return dict(entry)
    return None


def _record_processed_video(video_id: str | None) -> None:
    if not video_id:
        return
    log_file = Path(__file__).resolve().parents[2] / "processed_log.json"
    log = load_json(str(log_file), {"processed_file_ids": []})
    processed = log.setdefault("processed_file_ids", [])
    if video_id not in processed:
        processed.append(video_id)
        save_json(str(log_file), log)


def _claim_job(schedule_id: str, platform: str) -> dict | None:
    now = datetime.now().astimezone()
    with QUEUE_LOCK:
        queue = _load_queue()
        for index, entry in enumerate(queue):
            if str(entry.get("schedule_id") or entry.get("id")) != schedule_id:
                continue
            current_status = _platform_status(entry, platform)
            if current_status == "processing":
                started = entry.get(f"{platform}_processing_at")
                if started:
                    try:
                        started_at = datetime.fromisoformat(started)
                    except ValueError:
                        started_at = now - PROCESSING_TIMEOUT
                    if now - started_at < PROCESSING_TIMEOUT:
                        return None
                current_status = "scheduled"
            if current_status != "scheduled":
                return None
            scheduled_at = _scheduled_at(entry, platform)
            if scheduled_at is None or scheduled_at > now:
                return None
            entry.setdefault(platform, {})["status"] = "processing"
            entry[f"{platform}_status"] = "processing"
            entry[f"{platform}_processing_at"] = now.isoformat()
            entry["status"] = "processing"
            entry["updated_at"] = now.isoformat()
            queue[index] = entry
            _save_queue(queue)
            return dict(entry)
    return None


def _download_video(entry: dict) -> str:
    from googleapiclient.discovery import build

    from auth import get_credentials
    from upload_unlisted import download_video

    drive_file_id = entry.get("drive_file_id") or entry.get("video_id")
    if not drive_file_id:
        raise ValueError("Scheduled job is missing its Google Drive file ID")
    drive = build("drive", "v3", credentials=get_credentials())
    filename = f"{entry['schedule_id']}_{entry.get('file_name') or entry.get('filename') or 'video.mp4'}"
    return download_video(drive, drive_file_id, filename)


def _metadata(entry: dict, video_path: str) -> dict:
    from metadata_generator import METADATA_ANALYSIS_VERSION, generate_metadata

    with _METADATA_LOCK:
        queue_entry = next(
            (
                item
                for item in _load_queue()
                if str(item.get("schedule_id") or item.get("id"))
                == str(entry["schedule_id"])
            ),
            entry,
        )
        if (
            queue_entry.get("metadata_analysis_version") == METADATA_ANALYSIS_VERSION
            and queue_entry.get("title")
            and queue_entry.get("description")
        ):
            result = {
                "title": queue_entry["title"],
                "description": queue_entry["description"],
                "tags": queue_entry.get("tags") or [],
            }
            if not result["tags"]:
                result["tags"] = _local_metadata_tags(
                    queue_entry.get("file_name")
                    or queue_entry.get("filename")
                    or "video.mp4",
                    result["title"],
                    result["description"],
                )
                updated = _change_job(
                    entry["schedule_id"],
                    {
                        "tags": result["tags"],
                        "metadata_analysis_version": METADATA_ANALYSIS_VERSION,
                    },
                )
                if updated is None:
                    raise RuntimeError(
                        "Cached metadata tags could not be saved to the publishing "
                        "queue item."
                    )
            return result

        result = generate_metadata(
            queue_entry.get("file_name")
            or queue_entry.get("filename")
            or "video.mp4",
            video_path=video_path,
        )
        updated = _change_job(
            entry["schedule_id"],
            {
                "title": result["title"],
                "description": result["description"],
                "tags": result.get("tags")
                or _local_metadata_tags(
                    queue_entry.get("file_name") or queue_entry.get("filename") or "video.mp4",
                    result["title"],
                    result["description"],
                ),
                "metadata_analysis_version": METADATA_ANALYSIS_VERSION,
            },
        )
        if updated is None:
            raise RuntimeError(
                "Generated video metadata could not be saved to its publishing "
                "queue item; refusing to continue without a reusable cache."
            )
        return {
            "title": result["title"],
            "description": result["description"],
            "tags": result.get("tags") or updated["tags"],
        }


def _local_metadata_tags(filename: str, title: str, description: str) -> list[str]:
    import re

    tags = []
    seen = set()
    source = f"{title} {description} {os.path.splitext(os.path.basename(filename))[0]}"
    for value in re.findall(r"[\w'-]+", source.lower()):
        if len(value) < 3 or value in seen:
            continue
        seen.add(value)
        tags.append(value)
        if len(tags) >= 8:
            break
    return tags or ["video", "shorts", "content"]


def _deliver_youtube(entry: dict) -> None:
    from googleapiclient.discovery import build

    from auth import get_credentials
    from upload_unlisted import upload_unlisted_youtube

    video_id = entry.get("youtube_video_id")
    if not video_id:
        video_path = _download_video(entry)
        try:
            metadata = _metadata(entry, video_path)
            youtube = build("youtube", "v3", credentials=get_credentials())
            video_id = upload_unlisted_youtube(
                youtube,
                video_path,
                metadata["title"],
                metadata["description"],
                metadata["tags"],
            )
        finally:
            if os.path.exists(video_path):
                os.remove(video_path)
        _change_job(entry["schedule_id"], {"youtube_video_id": video_id})

    youtube = build("youtube", "v3", credentials=get_credentials())
    youtube.videos().update(
        part="status",
        body={"id": video_id, "status": {"privacyStatus": "public"}},
    ).execute()
    _set_platform_status(entry["schedule_id"], "youtube", "published")


def _deliver_telegram(entry: dict) -> None:
    from services.telegram import TelegramClient, build_video_caption

    video_path = _download_video(entry)
    try:
        metadata = _metadata(entry, video_path)
        client = TelegramClient.from_env()
        message_id = client.send_video(
            video_path,
            build_video_caption(metadata["title"], metadata["description"]),
        )
    finally:
        if os.path.exists(video_path):
            os.remove(video_path)
    _set_platform_status(
        entry["schedule_id"],
        "telegram",
        "sent",
        {"telegram_message_id": message_id},
    )


def _deliver_instagram(entry: dict) -> None:
    from backend.services.social_browser_service import social_browser
    from services.instagram_selenium import upload_instagram

    video_path = _download_video(entry)
    try:
        metadata = _metadata(entry, video_path)
        caption = f"{metadata['title']}\n\n{metadata['description']}"
        with social_browser("instagram") as driver:
            upload_instagram(
                video_path,
                caption,
                publish=True,
                driver=driver,
            )
    finally:
        if os.path.exists(video_path):
            os.remove(video_path)
    _set_platform_status(entry["schedule_id"], "instagram", "published")


def _deliver_facebook(entry: dict) -> None:
    from backend.services.social_browser_service import social_browser
    from services.facebook_selenium import upload_facebook

    page_url = entry.get("facebook_page_url")
    if not page_url:
        raise ValueError(
            "Facebook Page URL is missing. Configure it in Automation settings "
            "and reschedule this video."
        )
    video_path = _download_video(entry)
    try:
        metadata = _metadata(entry, video_path)
        caption = f"{metadata['title']}\n\n{metadata['description']}"
        with social_browser("facebook") as driver:
            upload_facebook(
                video_path,
                caption,
                page_url=page_url,
                publish=True,
                driver=driver,
            )
    finally:
        if os.path.exists(video_path):
            os.remove(video_path)
    _set_platform_status(entry["schedule_id"], "facebook", "published")


def _process_platform(schedule_id: str, platform: str) -> None:
    entry = _claim_job(schedule_id, platform)
    if entry is None:
        return
    try:
        if platform == "youtube":
            _deliver_youtube(entry)
        elif platform == "telegram":
            _deliver_telegram(entry)
        elif platform == "instagram":
            _deliver_instagram(entry)
        elif platform == "facebook":
            _deliver_facebook(entry)
        else:
            raise ValueError(f"Unsupported publishing platform: {platform}")
    except Exception as exc:
        logger.exception("Scheduled %s delivery failed for %s", platform, schedule_id)
        _set_platform_status(
            schedule_id,
            platform,
            "failed",
            {"last_error": f"{platform.capitalize()} delivery failed: {type(exc).__name__}: {exc}"},
        )


def process_due_schedules(now: datetime | None = None) -> None:
    now = now or datetime.now().astimezone()
    queue = _load_queue()
    for item in queue:
        schedule = normalize_schedule(item)
        if schedule["status"] not in {"scheduled", "processing"}:
            continue
        schedule_id = schedule["schedule_id"]
        for platform in schedule["platforms"]:
            if _platform_status(item, platform) not in {"scheduled", "processing"}:
                continue
            due = _scheduled_at(item, platform)
            if due is not None and due <= now:
                _process_platform(schedule_id, platform)
