"""
STEP 2 of the pipeline. Run this on a frequent schedule (e.g. every 10-15 min
via Task Scheduler or GitHub Actions). It checks publish_queue.json for anything
whose go_live_at time has arrived, then:

    1. Flips the YouTube video from unlisted -> public
    2. Prunes old published entries from the queue (keeps file size bounded)

    python publish_scheduled.py
"""

import logging
import os
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from googleapiclient.discovery import build

from auth import get_credentials
from utils import load_json, save_json

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

TIMEZONE_STR = os.environ.get("TIMEZONE", "Asia/Kolkata")
QUEUE_FILE = "publish_queue.json"

# Prune published entries older than this many days to keep the queue file small
QUEUE_PRUNE_DAYS = int(os.environ.get("QUEUE_PRUNE_DAYS", "30"))


# ---------------------------------------------------------------------------
# YouTube helpers
# ---------------------------------------------------------------------------

def set_youtube_public(youtube, video_id: str) -> None:
    """Flip *video_id* from unlisted to public."""
    youtube.videos().update(
        part="status",
        body={"id": video_id, "status": {"privacyStatus": "public"}},
    ).execute()


# ---------------------------------------------------------------------------
# Queue helpers
# ---------------------------------------------------------------------------

def prune_queue(queue: list[dict]) -> list[dict]:
    """Remove completed entries older than QUEUE_PRUNE_DAYS."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=QUEUE_PRUNE_DAYS)
    before = len(queue)

    def keep(item: dict) -> bool:
        telegram_pending = item.get("telegram_status") == "pending"
        if not item.get("published") or telegram_pending:
            return True

        timestamp = (
            item.get("go_live_at")
            or item.get("telegram_sent_at")
            or item.get("created_at")
        )
        if not timestamp:
            return True
        return datetime.fromisoformat(timestamp) >= cutoff

    queue = [item for item in queue if keep(item)]
    pruned = before - len(queue)
    if pruned:
        logger.info("Pruned %d old published entries from queue.", pruned)
    return queue


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(TIMEZONE_STR)
    now = datetime.now(tz)

    queue = load_json(QUEUE_FILE, [])
    due = [
        item for item in queue
        if not item.get("published")
        and item.get("youtube_video_id")
        and item.get("go_live_at")
        and datetime.fromisoformat(item["go_live_at"]) <= now
    ]

    if not due:
        logger.info("Nothing due yet.")
        return

    creds = get_credentials()
    youtube = build("youtube", "v3", credentials=creds)

    for item in due:
        yt_id = item["youtube_video_id"]
        logger.info("Publishing slot %s video: %s", item["slot"], yt_id)

        logger.info("  Setting YouTube to public...")
        set_youtube_public(youtube, yt_id)

        item["published"] = True
        item["youtube_status"] = "published"

    # Prune old entries and persist
    queue = prune_queue(queue)
    save_json(QUEUE_FILE, queue)
    logger.info("Done.")


if __name__ == "__main__":
    main()
