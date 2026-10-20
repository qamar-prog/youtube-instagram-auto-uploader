from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from pydantic import ValidationError
from unittest.mock import patch

from backend.routes import drive_automation as drive_automation_route
from backend.services import drive_automation_service, scheduler_service


class DriveAutomationTests(unittest.TestCase):
    def test_custom_interval_accepts_arbitrary_minutes_within_one_day(self):
        settings = drive_automation_service.DriveAutomationSettings(
            interval_minutes=17,
        )

        self.assertEqual(settings.interval_minutes, 17)
        with self.assertRaises(ValidationError):
            drive_automation_service.DriveAutomationSettings(interval_minutes=0)
        with self.assertRaises(ValidationError):
            drive_automation_service.DriveAutomationSettings(interval_minutes=1441)

    def test_custom_interval_is_used_when_assigning_the_next_schedule(self):
        settings = drive_automation_service.DriveAutomationSettings(
            interval_minutes=17,
            timezone="UTC",
        )
        now = datetime(2030, 1, 1, 10, 0, tzinfo=timezone.utc)

        schedule = drive_automation_service._next_platform_schedule(
            "instagram",
            settings,
            now,
        )

        self.assertEqual(schedule.date, "2030-01-01")
        self.assertEqual(schedule.time, "10:17")

    def test_saving_enabled_settings_immediately_queues_available_drive_videos(self):
        settings = drive_automation_service.DriveAutomationSettings(
            enabled=True,
            interval_minutes=17,
            max_videos_per_day=50,
        )
        saved = settings.model_dump()
        with (
            patch.object(
                drive_automation_route,
                "save_drive_automation_settings",
                return_value=saved,
            ),
            patch.object(
                drive_automation_route,
                "process_new_drive_videos",
                return_value=4,
            ) as process_videos,
        ):
            response = asyncio.run(drive_automation_route.put_settings(settings))

        process_videos.assert_called_once_with()
        self.assertEqual(response["settings"], saved)
        self.assertEqual(response["scheduled_count"], 4)
        self.assertIsNone(response["scheduling_error"])

    def test_new_videos_are_scheduled_at_the_selected_cadence_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings_path = root / "drive_automation_settings.json"
            state_path = root / "drive_automation_state.json"
            queue_path = root / "publish_queue.json"
            queue_path.write_text("[]", encoding="utf-8")

            with (
                patch.object(
                    drive_automation_service,
                    "DRIVE_AUTOMATION_SETTINGS_FILE",
                    settings_path,
                ),
                patch.object(
                    drive_automation_service,
                    "DRIVE_AUTOMATION_STATE_FILE",
                    state_path,
                ),
                patch.object(scheduler_service, "QUEUE_FILE", queue_path),
            ):
                drive_automation_service.save_drive_automation_settings(
                    drive_automation_service.DriveAutomationSettings(
                        enabled=True,
                        interval_minutes=30,
                        timezone="UTC",
                    )
                )
                state = json.loads(state_path.read_text(encoding="utf-8"))
                enabled_at = datetime.fromisoformat(state["enabled_since"])
                videos = [
                    {
                        "id": f"video-{index}",
                        "drive_file_id": f"video-{index}",
                        "filename": f"video-{index}.mp4",
                        "created_at": (enabled_at + timedelta(seconds=index + 1))
                        .astimezone(timezone.utc)
                        .isoformat(),
                    }
                    for index in (1, 2)
                ]

                def create_schedule(payload):
                    queue = scheduler_service._load_queue()
                    queue.append(
                        {
                            "schedule_id": payload.video_id,
                            "video_id": payload.video_id,
                            "drive_file_id": payload.video_id,
                            "platforms": payload.platforms,
                            "youtube": payload.youtube.model_dump(),
                            "timezone": payload.timezone,
                            "status": "scheduled",
                        }
                    )
                    scheduler_service._save_queue(queue)
                    return {"schedule_id": payload.video_id}

                with (
                    patch.object(
                        drive_automation_service,
                        "list_drive_videos",
                        return_value=videos,
                    ),
                    patch.object(
                        drive_automation_service,
                        "reserved_video_ids",
                        side_effect=[set(), {"video-1", "video-2"}],
                    ),
                    patch.object(
                        drive_automation_service,
                        "create_schedule",
                        side_effect=create_schedule,
                    ),
                ):
                    current = datetime(2030, 1, 1, 10, 0, tzinfo=timezone.utc)
                    scheduled_count = drive_automation_service.process_new_drive_videos(
                        current
                    )
                    second_pass_count = drive_automation_service.process_new_drive_videos(
                        current
                    )

            queue = json.loads(queue_path.read_text(encoding="utf-8"))

        self.assertEqual(scheduled_count, 2)
        self.assertEqual(second_pass_count, 0)
        self.assertEqual(
            [item["youtube"]["time"] for item in queue],
            ["10:30", "11:00"],
        )
        self.assertEqual([item["video_id"] for item in queue], ["video-1", "video-2"])

    def test_existing_available_drive_videos_are_queued_after_activation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings_path = root / "drive_automation_settings.json"
            state_path = root / "drive_automation_state.json"
            with (
                patch.object(
                    drive_automation_service,
                    "DRIVE_AUTOMATION_SETTINGS_FILE",
                    settings_path,
                ),
                patch.object(
                    drive_automation_service,
                    "DRIVE_AUTOMATION_STATE_FILE",
                    state_path,
                ),
            ):
                drive_automation_service.save_drive_automation_settings(
                    drive_automation_service.DriveAutomationSettings(enabled=True)
                )
                state = json.loads(state_path.read_text(encoding="utf-8"))
                state["scheduled_video_ids"] = ["old-video"]
                state_path.write_text(json.dumps(state), encoding="utf-8")
                queue_path = root / "publish_queue.json"
                queue_path.write_text("[]", encoding="utf-8")

                def create_schedule(payload):
                    queue = json.loads(queue_path.read_text(encoding="utf-8"))
                    queue.append(
                        {
                            "schedule_id": "scheduled-old-video",
                            "video_id": payload.video_id,
                            "drive_file_id": payload.video_id,
                            "platforms": payload.platforms,
                            "youtube": (
                                payload.youtube.model_dump()
                                if payload.youtube
                                else None
                            ),
                            "timezone": payload.timezone,
                            "status": "scheduled",
                        }
                    )
                    queue_path.write_text(json.dumps(queue), encoding="utf-8")
                    return {"schedule_id": "scheduled-old-video"}

                with (
                    patch.object(scheduler_service, "QUEUE_FILE", queue_path),
                    patch.object(
                        drive_automation_service,
                        "list_drive_videos",
                        return_value=[
                            {
                                "id": "old-video",
                                "drive_file_id": "old-video",
                                "created_at": "2026-10-01T12:00:00Z",
                            }
                        ],
                    ),
                    patch.object(
                        drive_automation_service,
                        "reserved_video_ids",
                        return_value=set(),
                    ),
                    patch.object(
                        drive_automation_service,
                        "create_schedule",
                        side_effect=create_schedule,
                    ) as create,
                ):
                    count = drive_automation_service.process_new_drive_videos()
                queue = json.loads(queue_path.read_text(encoding="utf-8"))

        self.assertEqual(count, 1)
        self.assertEqual(create.call_args.args[0].video_id, "old-video")
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["video_id"], "old-video")

    def test_daily_video_cap_moves_excess_drive_schedules_to_next_day(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings_path = root / "drive_automation_settings.json"
            state_path = root / "drive_automation_state.json"
            queue_path = root / "publish_queue.json"
            queue_path.write_text("[]", encoding="utf-8")
            with (
                patch.object(
                    drive_automation_service,
                    "DRIVE_AUTOMATION_SETTINGS_FILE",
                    settings_path,
                ),
                patch.object(
                    drive_automation_service,
                    "DRIVE_AUTOMATION_STATE_FILE",
                    state_path,
                ),
                patch.object(scheduler_service, "QUEUE_FILE", queue_path),
            ):
                drive_automation_service.save_drive_automation_settings(
                    drive_automation_service.DriveAutomationSettings(
                        enabled=True,
                        interval_minutes=30,
                        max_videos_per_day=2,
                        timezone="UTC",
                    )
                )
                videos = [
                    {"id": f"video-{index}", "drive_file_id": f"video-{index}"}
                    for index in range(3)
                ]

                def create_schedule(payload):
                    queue = scheduler_service._load_queue()
                    queue.append(
                        {
                            "schedule_id": payload.video_id,
                            "video_id": payload.video_id,
                            "drive_file_id": payload.video_id,
                            "platforms": payload.platforms,
                            "youtube": payload.youtube.model_dump(),
                            "timezone": payload.timezone,
                            "status": "scheduled",
                        }
                    )
                    scheduler_service._save_queue(queue)
                    return {"schedule_id": payload.video_id}

                with (
                    patch.object(
                        drive_automation_service,
                        "list_drive_videos",
                        return_value=videos,
                    ),
                    patch.object(
                        drive_automation_service,
                        "reserved_video_ids",
                        return_value=set(),
                    ),
                    patch.object(
                        drive_automation_service,
                        "create_schedule",
                        side_effect=create_schedule,
                    ),
                ):
                    count = drive_automation_service.process_new_drive_videos(
                        datetime(2030, 1, 1, 10, 0, tzinfo=timezone.utc)
                    )

            queue = json.loads(queue_path.read_text(encoding="utf-8"))

        self.assertEqual(count, 3)
        self.assertEqual(
            [(item["youtube"]["date"], item["youtube"]["time"]) for item in queue],
            [
                ("2030-01-01", "10:30"),
                ("2030-01-01", "11:00"),
                ("2030-01-02", "11:30"),
            ],
        )

    def test_daily_video_cap_must_be_between_one_and_fifty(self):
        for value in (0, 51):
            with self.subTest(value=value), self.assertRaises(ValueError):
                drive_automation_service.DriveAutomationSettings(
                    max_videos_per_day=value
                )


if __name__ == "__main__":
    unittest.main()
