import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from backend.services import publishing_worker, scheduler_service


class PublishingWorkerTests(unittest.TestCase):
    def test_stale_cached_metadata_is_regenerated_and_saved_to_queue(self):
        entry = {
            "schedule_id": "workplace-safety-job",
            "file_name": "Video_about_workplace_safety_AI_20261013000213.mp4",
            "title": "Stewie Takes Over the Minecraft Obby!",
            "description": "A cartoon gaming description unrelated to this video.",
            "tags": ["minecraft", "family guy"],
        }
        generated = {
            "title": "Computer Vision Improves Workplace Safety",
            "description": "AI detects workplace hazards. #ComputerVision #AI #Safety",
            "tags": ["computer vision", "workplace safety", "AI"],
        }
        queue = [entry]

        def save_metadata(schedule_id, changes):
            queue[0].update(changes)
            return queue[0]

        with (
            patch(
                "metadata_generator.generate_metadata",
                return_value=generated,
            ) as generate,
            patch.object(publishing_worker, "_load_queue", return_value=queue),
            patch.object(
                publishing_worker,
                "_change_job",
                side_effect=save_metadata,
            ) as save,
        ):
            result = publishing_worker._metadata(entry, "downloaded-video.mp4")

        self.assertEqual(result, generated)
        generate.assert_called_once_with(
            entry["file_name"],
            video_path="downloaded-video.mp4",
        )
        self.assertEqual(
            save.call_args.args[1]["metadata_analysis_version"],
            3,
        )
        self.assertEqual(
            save.call_args.args[1]["title"],
            "Computer Vision Improves Workplace Safety",
        )

    def test_metadata_is_generated_once_and_reused_across_destinations(self):
        entry = {
            "schedule_id": "shared-video-job",
            "file_name": "shared-video.mp4",
        }
        queue = [entry]
        generated = {
            "title": "One shared title",
            "description": "One shared description",
            "tags": ["shared"],
        }

        def save_metadata(_schedule_id, changes):
            queue[0].update(changes)
            return queue[0]

        with (
            patch.object(publishing_worker, "_load_queue", return_value=queue),
            patch.object(
                publishing_worker,
                "_change_job",
                side_effect=save_metadata,
            ),
            patch(
                "metadata_generator.generate_metadata",
                return_value=generated,
            ) as generate,
        ):
            first_destination = publishing_worker._metadata(entry, "first-download.mp4")
            second_destination = publishing_worker._metadata(entry, "second-download.mp4")

        self.assertEqual(first_destination, generated)
        self.assertEqual(second_destination, generated)
        generate.assert_called_once_with(
            "shared-video.mp4",
            video_path="first-download.mp4",
        )
    def test_failed_delivery_records_the_platform_in_its_error(self):
        with tempfile.TemporaryDirectory() as directory:
            queue_file = Path(directory) / "publish_queue.json"
            due = datetime.now(ZoneInfo("Asia/Karachi")) - timedelta(minutes=2)
            queue_file.write_text(
                json.dumps(
                    [
                        {
                            "schedule_id": "failed-telegram-job",
                            "video_id": "drive-video",
                            "drive_file_id": "drive-video",
                            "file_name": "clip.mp4",
                            "platforms": ["telegram"],
                            "telegram": {
                                "date": due.date().isoformat(),
                                "time": due.strftime("%H:%M"),
                                "status": "scheduled",
                            },
                            "timezone": "Asia/Karachi",
                            "status": "scheduled",
                        }
                    ]
                ),
                encoding="utf-8",
            )

            with (
                patch.object(scheduler_service, "QUEUE_FILE", queue_file),
                patch.object(
                    publishing_worker,
                    "_deliver_telegram",
                    side_effect=TimeoutError("connection timed out"),
                ),
            ):
                publishing_worker.process_due_schedules()
                stored = json.loads(queue_file.read_text(encoding="utf-8"))[0]

        self.assertEqual(stored["telegram"]["status"], "failed")
        self.assertIn("Telegram delivery failed: TimeoutError", stored["last_error"])

    def test_youtube_job_uploads_from_legacy_video_id_and_sets_public(self):
        with tempfile.TemporaryDirectory() as directory:
            queue_file = Path(directory) / "publish_queue.json"
            video_path = Path(directory) / "video.mp4"
            video_path.touch()
            queue_file.write_text(
                json.dumps(
                    [
                        {
                            "schedule_id": "legacy-job",
                            "video_id": "drive-file-id",
                            "filename": "video.mp4",
                            "platforms": ["youtube"],
                            "youtube": {
                                "date": "2026-10-01",
                                "time": "12:00",
                                "status": "processing",
                            },
                            "timezone": "Asia/Karachi",
                            "status": "processing",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            youtube = Mock()
            credentials = Mock()
            metadata = {
                "title": "Video title",
                "description": "Video description",
                "tags": ["video"],
            }

            with (
                patch.object(scheduler_service, "QUEUE_FILE", queue_file),
                patch("auth.get_credentials", return_value=credentials),
                patch("googleapiclient.discovery.build", return_value=youtube),
                patch(
                    "upload_unlisted.download_video",
                    return_value=str(video_path),
                ) as download,
                patch(
                    "metadata_generator.generate_metadata",
                    return_value=metadata,
                ),
                patch(
                    "upload_unlisted.upload_unlisted_youtube",
                    return_value="youtube-video-id",
                ) as upload,
                patch.object(publishing_worker, "_record_processed_video"),
            ):
                publishing_worker._deliver_youtube(
                    json.loads(queue_file.read_text(encoding="utf-8"))[0]
                )
                stored = json.loads(queue_file.read_text(encoding="utf-8"))[0]

            download.assert_called_once()
            self.assertEqual(download.call_args.args[1], "drive-file-id")
            upload.assert_called_once()
            youtube.videos.return_value.update.assert_called_once_with(
                part="status",
                body={
                    "id": "youtube-video-id",
                    "status": {"privacyStatus": "public"},
                },
            )
            self.assertEqual(stored["status"], "completed")
            self.assertFalse(video_path.exists())

    def test_due_job_is_delivered_and_disappears_from_active_schedules(self):
        with tempfile.TemporaryDirectory() as directory:
            queue_file = Path(directory) / "publish_queue.json"
            due = datetime.now(ZoneInfo("Asia/Karachi")) - timedelta(minutes=2)
            queue_file.write_text(
                json.dumps(
                    [
                        {
                            "schedule_id": "due-job",
                            "video_id": "drive-video",
                            "drive_file_id": "drive-video",
                            "file_name": "clip.mp4",
                            "platforms": ["youtube"],
                            "youtube": {
                                "date": due.date().isoformat(),
                                "time": due.strftime("%H:%M"),
                                "status": "scheduled",
                            },
                            "timezone": "Asia/Karachi",
                            "status": "scheduled",
                        }
                    ]
                ),
                encoding="utf-8",
            )

            with (
                patch.object(scheduler_service, "QUEUE_FILE", queue_file),
                patch.object(
                    publishing_worker,
                    "_deliver_youtube",
                    side_effect=lambda entry: publishing_worker._set_platform_status(
                        entry["schedule_id"], "youtube", "published"
                    ),
                ),
                patch.object(publishing_worker, "_record_processed_video"),
            ):
                publishing_worker.process_due_schedules()
                self.assertEqual(scheduler_service.list_schedules(), [])
                stored = json.loads(queue_file.read_text(encoding="utf-8"))

            self.assertEqual(stored[0]["status"], "completed")
            self.assertEqual(stored[0]["youtube"]["status"], "published")

    def test_future_job_is_not_delivered(self):
        with tempfile.TemporaryDirectory() as directory:
            queue_file = Path(directory) / "publish_queue.json"
            future = datetime.now(ZoneInfo("Asia/Karachi")) + timedelta(days=2)
            queue_file.write_text(
                json.dumps(
                    [
                        {
                            "schedule_id": "future-job",
                            "video_id": "drive-video",
                            "platforms": ["youtube"],
                            "youtube": {
                                "date": future.date().isoformat(),
                                "time": future.strftime("%H:%M"),
                                "status": "scheduled",
                            },
                            "timezone": "Asia/Karachi",
                            "status": "scheduled",
                        }
                    ]
                ),
                encoding="utf-8",
            )

            with (
                patch.object(scheduler_service, "QUEUE_FILE", queue_file),
                patch.object(publishing_worker, "_deliver_youtube") as deliver,
            ):
                publishing_worker.process_due_schedules()

            deliver.assert_not_called()


if __name__ == "__main__":
    unittest.main()
