"""
STEP 1 of the pipeline. Run this 8 hours before a scheduled slot time.

Picks the next unprocessed video from Drive, generates metadata with Groq,
uploads it to YouTube as UNLISTED (visible via link, not searchable), and
queues it in publish_queue.json with the target public/live time.

    python upload_unlisted.py --slot A
    python upload_unlisted.py --slot B

Slot times are read from .env (SLOT_A_TIME, SLOT_B_TIME, daily, in TIMEZONE).
"""

import argparse
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

from auth import get_credentials
from metadata_generator import generate_metadata
from services.telegram import TelegramClient, build_video_caption
from utils import load_json, save_json

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DRIVE_FOLDER_ID = os.environ["DRIVE_FOLDER_ID"]
TIMEZONE = os.environ.get("TIMEZONE", "Asia/Kolkata")
YOUTUBE_CATEGORY_ID = os.environ.get("YOUTUBE_CATEGORY_ID", "22")  # 22 = People & Blogs

SLOT_TIMES = {
    "A": os.environ.get("SLOT_A_TIME", "17:30"),  # 5:30 PM IST default
    "B": os.environ.get("SLOT_B_TIME", "21:30"),  # 9:30 PM IST default
}

LOG_FILE = "processed_log.json"
QUEUE_FILE = "publish_queue.json"
TEMP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "temp_downloads")


# ---------------------------------------------------------------------------
# Drive helpers
# ---------------------------------------------------------------------------

def list_drive_videos(drive, folder_id: str) -> list[dict]:
    query = f"'{folder_id}' in parents and trashed = false"
    results = (
        drive.files()
        .list(q=query, fields="files(id, name, mimeType)", orderBy="name", pageSize=1000)
        .execute()
    )
    return [f for f in results.get("files", []) if f["mimeType"].startswith("video/")]


def download_video(drive, file_id: str, filename: str) -> str:
    """Download *file_id* from Drive into TEMP_DIR and return the local path."""
    os.makedirs(TEMP_DIR, exist_ok=True)
    safe_name = filename.replace("\\", "/").rsplit("/", maxsplit=1)[-1]
    safe_name = re.sub(r'[<>:"/|?*\x00-\x1f]', "_", safe_name).strip(" .")
    if not safe_name:
        safe_name = f"{file_id}.video"
    local_path = os.path.join(TEMP_DIR, safe_name)
    request = drive.files().get_media(fileId=file_id)
    with open(local_path, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            status, done = downloader.next_chunk()
            if status:
                logger.info("  Download progress: %d%%", int(status.progress() * 100))
    return local_path


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------

def next_slot_datetime(slot: str) -> datetime:
    """Return the next occurrence of *slot*'s time in TIMEZONE (tomorrow if already past)."""
    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)
    hour, minute = map(int, SLOT_TIMES[slot].split(":"))
    target = datetime(now.year, now.month, now.day, hour, minute, tzinfo=tz)
    if target <= now:
        target += timedelta(days=1)
    return target


# ---------------------------------------------------------------------------
# YouTube helpers
# ---------------------------------------------------------------------------

def upload_unlisted_youtube(
    youtube, video_path: str, title: str, description: str, tags: list[str]
) -> str:
    """Upload *video_path* as an unlisted video and return the YouTube video ID."""
    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags,
            "categoryId": YOUTUBE_CATEGORY_ID,
        },
        "status": {
            "privacyStatus": "unlisted",
            "selfDeclaredMadeForKids": False,
        },
    }
    with open(video_path, "rb") as video_file:
        media = MediaIoBaseUpload(
            video_file, chunksize=-1, resumable=True, mimetype="video/*"
        )
        request = youtube.videos().insert(
            part="snippet,status", body=body, media_body=media
        )

        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                logger.info("  Upload progress: %d%%", int(status.progress() * 100))

    return response["id"]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Queue a video for a publishing slot.")
    parser.add_argument(
        "--slot",
        choices=["A", "B"],
        help="YouTube publishing slot (required when YouTube is a destination)",
    )
    parser.add_argument(
        "--destination",
        choices=["youtube", "telegram", "both"],
        default="youtube",
        help="Where to publish the next video (default: youtube)",
    )
    args = parser.parse_args()

    destinations = (
        ["youtube", "telegram"] if args.destination == "both" else [args.destination]
    )
    if "youtube" in destinations and not args.slot:
        parser.error("--slot is required when the destination includes YouTube")

    creds = get_credentials()
    drive = build("drive", "v3", credentials=creds)
    youtube = (
        build("youtube", "v3", credentials=creds)
        if "youtube" in destinations
        else None
    )

    log = load_json(LOG_FILE, {"processed_file_ids": []})
    queue = load_json(QUEUE_FILE, [])
    known_file_ids = set(log["processed_file_ids"])
    known_file_ids.update(
        item["drive_file_id"] for item in queue if item.get("drive_file_id")
    )

    telegram_client = None
    if "telegram" in destinations or any(
        item.get("telegram_status") == "pending" for item in queue
    ):
        telegram_client = TelegramClient.from_env()

    if telegram_client:
        retry_pending_telegram(drive, queue, telegram_client)

    videos = list_drive_videos(drive, DRIVE_FOLDER_ID)
    next_video = next(
        (v for v in videos if v["id"] not in known_file_ids),
        None,
    )

    if not next_video:
        logger.info("No new videos to process.")
        return

    logger.info("Next video: %s", next_video["name"])

    logger.info("Downloading from Drive...")
    local_path = download_video(drive, next_video["id"], next_video["name"])

    try:
        logger.info("Generating metadata from video frames with Groq...")
        metadata = generate_metadata(next_video["name"], video_path=local_path)
        now = datetime.now(timezone.utc).isoformat()
        queue_entry = {
            "drive_file_id": next_video["id"],
            "file_name": next_video["name"],
            "title": metadata["title"],
            "description": metadata["description"],
            "destinations": destinations,
            "youtube_status": "scheduled" if "youtube" in destinations else "not_selected",
            "telegram_status": "pending" if "telegram" in destinations else "not_selected",
            "created_at": now,
            "published": "youtube" not in destinations,
        }

        if "youtube" in destinations:
            logger.info("Uploading to YouTube as unlisted...")
            video_id = upload_unlisted_youtube(
                youtube, local_path,
                metadata["title"], metadata["description"], metadata["tags"],
            )
            logger.info("  YouTube video ID: %s", video_id)

            go_live_at = next_slot_datetime(args.slot)
            logger.info("Queued to go public/live at: %s", go_live_at.isoformat())
            queue_entry.update({
                "youtube_video_id": video_id,
                "go_live_at": go_live_at.isoformat(),
                "slot": args.slot,
            })

        queue.append(queue_entry)
        log["processed_file_ids"].append(next_video["id"])
        save_json(QUEUE_FILE, queue)
        save_json(LOG_FILE, log)

        if "telegram" in destinations:
            send_telegram_video(
                local_path, queue_entry, telegram_client, queue
            )

    finally:
        if os.path.exists(local_path):
            os.remove(local_path)
            logger.debug("Removed temp file: %s", local_path)

    logger.info("Done. Destinations: %s", ", ".join(destinations))


def send_telegram_video(
    local_path: str,
    queue_entry: dict,
    telegram_client: TelegramClient,
    queue: list[dict],
) -> None:
    """Send a queued video to Telegram and persist its delivery status."""
    message_id = telegram_client.send_video(
        local_path,
        build_video_caption(queue_entry["title"], queue_entry["description"]),
    )
    queue_entry["telegram_status"] = "sent"
    queue_entry["telegram_message_id"] = message_id
    queue_entry["telegram_sent_at"] = datetime.now(timezone.utc).isoformat()
    save_json(QUEUE_FILE, queue)
    logger.info("  Telegram message ID: %s", message_id)


def retry_pending_telegram(
    drive, queue: list[dict], telegram_client: TelegramClient
) -> None:
    """Retry Telegram deliveries left pending by an interrupted prior run."""
    for entry in queue:
        if entry.get("telegram_status") != "pending":
            continue

        logger.info("Retrying pending Telegram delivery: %s", entry["file_name"])
        local_path = download_video(drive, entry["drive_file_id"], entry["file_name"])
        try:
            send_telegram_video(local_path, entry, telegram_client, queue)
        finally:
            if os.path.exists(local_path):
                os.remove(local_path)


if __name__ == "__main__":
    main()