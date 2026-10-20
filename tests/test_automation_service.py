from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from backend.models.schedule import PlatformSchedule
from backend.services import automation_service, scheduler_service


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        fixed = datetime(2030, 1, 1, 16, 0)
        return fixed.replace(tzinfo=tz) if tz else fixed


class FakeResponse:
    ok = True
    status_code = 200
    text = ""

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class AutomationQueueTests(unittest.TestCase):
    def test_enqueue_and_retry_jobs_are_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            queue_path = Path(directory) / "automation_queue.json"
            with patch.object(automation_service, "AUTOMATION_QUEUE_FILE", queue_path):
                jobs = automation_service.enqueue_prompts(
                    automation_service.PromptBatch(
                        prompts=["A useful video topic about travel"]
                    )
                )
                stored = json.loads(queue_path.read_text(encoding="utf-8"))
                stored[0]["status"] = "failed"
                queue_path.write_text(json.dumps(stored), encoding="utf-8")

                retried = automation_service.retry_automation_job(jobs[0]["job_id"])

            self.assertEqual(retried["status"], "queued")
            self.assertEqual(retried["prompt"], "A useful video topic about travel")

    def test_next_slot_skips_an_existing_schedule(self):
        with tempfile.TemporaryDirectory() as directory:
            queue_path = Path(directory) / "publish_queue.json"
            queue_path.write_text(
                json.dumps(
                    [
                        {
                            "schedule_id": "already-booked",
                            "video_id": "drive-video",
                            "drive_file_id": "drive-video",
                            "platforms": ["youtube"],
                            "youtube": {
                                "date": "2030-01-01",
                                "time": "17:30",
                                "status": "scheduled",
                            },
                            "status": "scheduled",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            settings = automation_service.AutomationSettings(
                platforms=["youtube"],
                timezone="Asia/Karachi",
                youtube_times=["17:30", "21:30"],
            )
            with (
                patch.object(scheduler_service, "QUEUE_FILE", queue_path),
                patch.object(automation_service, "datetime", FrozenDateTime),
            ):
                slot = automation_service._next_platform_slot("youtube", settings)

            self.assertEqual(slot.date, "2030-01-01")
            self.assertEqual(slot.time, "21:30")

    def test_scheduling_uses_saved_destination_defaults(self):
        settings = automation_service.AutomationSettings(
            platforms=["youtube", "telegram"],
            timezone="Asia/Karachi",
            youtube_times=["17:30"],
            telegram_times=["21:30"],
        )
        youtube = PlatformSchedule(date="2030-01-01", time="17:30")
        telegram = PlatformSchedule(date="2030-01-01", time="21:30")
        with (
            patch.object(
                automation_service,
                "_next_platform_slot",
                side_effect=[youtube, telegram],
            ),
            patch.object(
                automation_service,
                "create_schedule",
                return_value={"schedule_id": "saved-schedule"},
            ) as create_schedule,
            patch.object(automation_service, "_load_queue", return_value=[]),
        ):
            result = automation_service._schedule_drive_video(
                {"drive_file_id": "drive-video"}, settings
            )

        self.assertEqual(result["schedule_id"], "saved-schedule")
        payload = create_schedule.call_args.args[0]
        self.assertEqual(payload.platforms, ["youtube", "telegram"])
        self.assertEqual(payload.youtube.time, "17:30")
        self.assertEqual(payload.telegram.time, "21:30")

    def test_completed_render_is_uploaded_and_scheduled(self):
        with tempfile.TemporaryDirectory() as directory:
            queue_path = Path(directory) / "automation_queue.json"
            output_path = Path(directory) / "rendered.mp4"
            output_path.write_bytes(b"video")
            job = {
                "job_id": "automation-12345678",
                "prompt": "A travel advice short video",
                "status": "rendering",
                "generation_id": "generation-1",
                "generation_title": "Travel Advice",
            }
            queue_path.write_text(json.dumps([job]), encoding="utf-8")
            settings = automation_service.AutomationSettings(
                platforms=["youtube"],
                timezone="Asia/Karachi",
                youtube_times=["17:30"],
            )

            with (
                patch.object(automation_service, "AUTOMATION_QUEUE_FILE", queue_path),
                patch.object(
                    automation_service,
                    "GENERATOR_OUTPUT_DIR",
                    Path(directory).resolve(),
                ),
                patch.object(
                    automation_service.requests,
                    "get",
                    return_value=FakeResponse(
                        {
                            "status": "completed",
                            "output_path": str(output_path),
                            "title": "Travel Advice",
                        }
                    ),
                ),
                patch.object(
                    automation_service,
                    "find_drive_video_by_name",
                    return_value=None,
                ),
                patch.object(
                    automation_service,
                    "upload_video_to_drive",
                    return_value={"id": "drive-1", "name": "generated.mp4"},
                ) as upload,
                patch.object(
                    automation_service,
                    "_schedule_drive_video",
                    return_value={"schedule_id": "schedule-1"},
                ),
            ):
                automation_service._finish_generation(job, settings)

            saved_job = json.loads(queue_path.read_text(encoding="utf-8"))[0]

        self.assertEqual(saved_job["status"], "scheduled")
        self.assertEqual(saved_job["drive_file_id"], "drive-1")
        self.assertEqual(saved_job["schedule_id"], "schedule-1")
        self.assertEqual(saved_job["drive_filename"], "generated.mp4")
        self.assertEqual(upload.call_args.args[0], output_path)
        self.assertIn("Travel-Advice", upload.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
