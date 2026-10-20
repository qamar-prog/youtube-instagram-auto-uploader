import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from publish_scheduled import prune_queue
from services.telegram import build_video_caption
from telegram_bot import allowed_user_ids, process_callback


class TelegramCaptionTests(unittest.TestCase):
    def test_caption_contains_title_and_description(self):
        self.assertEqual(
            build_video_caption("A title", "A description"),
            "A title\n\nA description",
        )

    def test_caption_is_limited_to_telegram_length(self):
        caption = build_video_caption("Title", "x" * 2000)
        self.assertEqual(len(caption), 1024)
        self.assertTrue(caption.endswith("..."))

    def test_caption_respects_utf16_length_for_emoji(self):
        caption = build_video_caption("", "😀" * 600)
        self.assertLessEqual(len(caption.encode("utf-16-le")) // 2, 1024)
        self.assertTrue(caption.endswith("..."))


class TelegramAuthorizationTests(unittest.TestCase):
    def test_parses_multiple_allowed_ids(self):
        with patch.dict(
            "os.environ", {"TELEGRAM_ALLOWED_USER_IDS": "123, 456"}
        ):
            self.assertEqual(allowed_user_ids(), {123, 456})

    def test_rejects_non_numeric_allowed_ids(self):
        with patch.dict(
            "os.environ", {"TELEGRAM_ALLOWED_USER_IDS": "123,nope"}
        ):
            with self.assertRaisesRegex(ValueError, "numeric IDs"):
                allowed_user_ids()


class TelegramApiTests(unittest.TestCase):
    @patch("services.telegram.requests.post")
    def test_serializes_inline_keyboard_as_json(self, post):
        response = Mock()
        response.ok = True
        response.status_code = 200
        response.json.return_value = {"ok": True, "result": {}}
        post.return_value = response

        from services.telegram import TelegramClient

        client = TelegramClient("test-token")
        keyboard = {"inline_keyboard": [[{"text": "Publish", "callback_data": "go"}]]}
        client.send_message(123, "Choose:", keyboard)

        request_data = post.call_args.kwargs["data"]
        self.assertEqual(json.loads(request_data["reply_markup"]), keyboard)


class TelegramControlTests(unittest.TestCase):
    class FakeClient:
        def __init__(self):
            self.answered = []
            self.cleared = []

        def answer_callback_query(self, callback_id, text=""):
            self.answered.append((callback_id, text))

        def clear_inline_keyboard(self, chat_id, message_id):
            self.cleared.append((chat_id, message_id))

    def test_telegram_destination_starts_local_upload(self):
        client = self.FakeClient()
        callback = {
            "id": "callback-1",
            "from": {"id": 123},
            "message": {
                "message_id": 7,
                "chat": {"id": 123, "type": "private"},
            },
            "data": "dest:telegram",
        }

        with patch("telegram_bot.run_upload") as run_upload:
            process_callback(callback, client, {123}, set())

        run_upload.assert_called_once_with("telegram", None, 123, client)
        self.assertEqual(client.cleared, [(123, 7)])

    def test_repeated_selection_is_ignored(self):
        client = self.FakeClient()
        callback = {
            "id": "callback-1",
            "from": {"id": 123},
            "message": {
                "message_id": 7,
                "chat": {"id": 123, "type": "private"},
            },
            "data": "dest:telegram",
        }
        handled = set()

        with patch("telegram_bot.run_upload") as run_upload:
            process_callback(callback, client, {123}, handled)
            callback["id"] = "callback-2"
            process_callback(callback, client, {123}, handled)

        run_upload.assert_called_once()
        self.assertEqual(
            client.answered[-1],
            ("callback-2", "This selection was already used"),
        )


class QueuePruningTests(unittest.TestCase):
    def test_keeps_pending_telegram_delivery_and_prunes_old_completed_items(self):
        old = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
        recent = datetime.now(timezone.utc).isoformat()
        queue = [
            {
                "published": True,
                "telegram_status": "sent",
                "created_at": old,
            },
            {
                "published": True,
                "telegram_status": "pending",
                "created_at": old,
            },
            {
                "published": True,
                "telegram_status": "sent",
                "created_at": recent,
            },
        ]

        pruned = prune_queue(queue)

        self.assertEqual(len(pruned), 2)
        self.assertEqual(pruned[0]["telegram_status"], "pending")
        self.assertEqual(pruned[1]["created_at"], recent)


if __name__ == "__main__":
    unittest.main()
