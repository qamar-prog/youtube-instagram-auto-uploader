import json
import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from backend.models.schedule import PlatformSchedule, ScheduleUpdate
from backend.routes import videos as videos_route
from backend.services import scheduler_service


class LegacyScheduleIdTests(unittest.TestCase):
    def test_older_dashboard_jobs_gain_drive_id_from_video_id(self):
        with tempfile.TemporaryDirectory() as directory:
            queue_file = Path(directory) / "publish_queue.json"
            queue_file.write_text(
                json.dumps(
                    [
                        {
                            "schedule_id": "legacy-dashboard-job",
                            "video_id": "drive-video-1",
                            "filename": "video.mp4",
                        }
                    ]
                ),
                encoding="utf-8",
            )

            with patch.object(scheduler_service, "QUEUE_FILE", queue_file):
                migrated = scheduler_service._load_queue()

            self.assertEqual(migrated[0]["drive_file_id"], "drive-video-1")

    def test_legacy_schedule_id_persists_and_can_be_updated_and_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            queue_file = Path(directory) / "publish_queue.json"
            queue_file.write_text(
                json.dumps(
                    [
                        {
                            "drive_file_id": "drive-video-1",
                            "file_name": "video.mp4",
                            "destinations": ["telegram"],
                            "telegram_status": "pending",
                            "telegram": {
                                "date": (datetime.now(ZoneInfo("Asia/Karachi")) + timedelta(days=1)).date().isoformat(),
                                "time": "21:30",
                                "status": "pending",
                            },
                        }
                    ]
                ),
                encoding="utf-8",
            )

            with (
                patch.object(scheduler_service, "QUEUE_FILE", queue_file),
                patch.object(scheduler_service, "_processed_video_ids", return_value=set()),
                patch.object(
                    scheduler_service,
                    "get_drive_video",
                    return_value={
                        "filename": "video.mp4",
                        "thumbnail": "",
                    },
                ),
            ):
                listed = scheduler_service.list_schedules()
                schedule_id = listed[0]["schedule_id"]
                scheduled_date = (
                    datetime.now(ZoneInfo("Asia/Karachi")) + timedelta(days=2)
                ).date().isoformat()

                stored = json.loads(queue_file.read_text(encoding="utf-8"))
                self.assertEqual(stored[0]["schedule_id"], schedule_id)

                updated = scheduler_service.update_schedule(
                    schedule_id,
                    ScheduleUpdate(
                        video_id="drive-video-1",
                        platforms=["telegram"],
                        youtube=None,
                        telegram=PlatformSchedule(
                            date=scheduled_date,
                            time="21:30",
                            status="scheduled",
                        ),
                    ),
                )
                self.assertEqual(updated["schedule_id"], schedule_id)
                self.assertEqual(updated["telegram"]["date"], scheduled_date)
                self.assertEqual(updated["telegram"]["time"], "21:30")
                self.assertEqual(updated["video_id"], "drive-video-1")

                scheduler_service.delete_schedule(schedule_id)
                self.assertEqual(json.loads(queue_file.read_text(encoding="utf-8")), [])

    def test_completed_jobs_are_not_listed_as_scheduled(self):
        with tempfile.TemporaryDirectory() as directory:
            queue_file = Path(directory) / "publish_queue.json"
            queue_file.write_text(
                json.dumps(
                    [
                        {
                            "schedule_id": "completed",
                            "video_id": "done-video",
                            "platforms": ["youtube"],
                            "youtube": {
                                "date": "2026-10-01",
                                "time": "12:00",
                                "status": "published",
                            },
                            "status": "completed",
                            "published": True,
                        },
                        {
                            "schedule_id": "active",
                            "video_id": "pending-video",
                            "platforms": ["youtube"],
                            "youtube": {
                                "date": "2026-10-05",
                                "time": "12:00",
                                "status": "scheduled",
                            },
                            "status": "scheduled",
                        },
                    ]
                ),
                encoding="utf-8",
            )

            with patch.object(scheduler_service, "QUEUE_FILE", queue_file):
                schedules = scheduler_service.list_schedules()

            self.assertEqual([item["schedule_id"] for item in schedules], ["active"])


class AvailableDriveVideoTests(unittest.TestCase):
    def test_videos_with_existing_schedules_are_removed_from_available(self):
        drive_videos = [
            {"id": "reserved", "drive_file_id": "reserved", "filename": "scheduled.mp4"},
            {"id": "new", "drive_file_id": "new", "filename": "new.mp4"},
        ]
        with (
            patch.object(videos_route, "list_drive_videos", return_value=drive_videos),
            patch.object(videos_route, "reserved_video_ids", return_value={"reserved"}),
        ):
            result = asyncio.run(videos_route.get_videos())

        self.assertEqual([video["drive_file_id"] for video in result], ["new"])


class FailedScheduleDeletionTests(unittest.TestCase):
    def test_failed_schedule_can_be_removed_after_another_platform_published(self):
        with tempfile.TemporaryDirectory() as directory:
            queue_file = Path(directory) / "publish_queue.json"
            queue_file.write_text(
                json.dumps(
                    [
                        {
                            "schedule_id": "partially-delivered",
                            "drive_file_id": "drive-video",
                            "platforms": ["youtube", "telegram"],
                            "youtube": {"status": "published"},
                            "telegram": {"status": "failed"},
                            "status": "failed",
                        }
                    ]
                ),
                encoding="utf-8",
            )

            with patch.object(scheduler_service, "QUEUE_FILE", queue_file):
                scheduler_service.delete_schedule("partially-delivered")

            self.assertEqual(json.loads(queue_file.read_text(encoding="utf-8")), [])

    def test_job_cannot_be_removed_while_a_platform_is_processing(self):
        with tempfile.TemporaryDirectory() as directory:
            queue_file = Path(directory) / "publish_queue.json"
            queue_file.write_text(
                json.dumps(
                    [
                        {
                            "schedule_id": "still-processing",
                            "drive_file_id": "drive-video",
                            "platforms": ["youtube", "telegram"],
                            "youtube": {"status": "published"},
                            "telegram": {"status": "processing"},
                            "status": "processing",
                        }
                    ]
                ),
                encoding="utf-8",
            )

            with patch.object(scheduler_service, "QUEUE_FILE", queue_file):
                with self.assertRaisesRegex(ValueError, "while a delivery is processing"):
                    scheduler_service.delete_schedule("still-processing")


if __name__ == "__main__":
    unittest.main()
