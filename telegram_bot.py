"""Telegram control panel for choosing a destination for the next video."""

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from services.telegram import TelegramClient

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

DESTINATIONS = {
    "youtube": "YouTube",
    "telegram": "Telegram channel",
    "both": "YouTube and Telegram",
}


def allowed_user_ids() -> set[int]:
    configured = os.getenv("TELEGRAM_ALLOWED_USER_IDS", "").strip()
    if not configured:
        raise ValueError("TELEGRAM_ALLOWED_USER_IDS is not configured")
    try:
        user_ids = {int(value.strip()) for value in configured.split(",") if value.strip()}
    except ValueError as exc:
        raise ValueError(
            "TELEGRAM_ALLOWED_USER_IDS must be comma-separated numeric IDs"
        ) from exc
    if not user_ids:
        raise ValueError("TELEGRAM_ALLOWED_USER_IDS must contain at least one ID")
    return user_ids


def destination_keyboard() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "YouTube only", "callback_data": "dest:youtube"},
                {"text": "Telegram only", "callback_data": "dest:telegram"},
            ],
            [{"text": "Both", "callback_data": "dest:both"}],
        ]
    }


def slot_keyboard(destination: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {
                    "text": f"Slot {slot}",
                    "callback_data": f"slot:{slot}:{destination}",
                }
                for slot in ("A", "B")
            ]
        ]
    }


def process_update(
    update: dict[str, Any],
    client: TelegramClient,
    authorized_ids: set[int],
    handled_message_ids: set[tuple[int, int]],
) -> None:
    callback = update.get("callback_query")
    if callback:
        process_callback(callback, client, authorized_ids, handled_message_ids)
        return

    message = update.get("message")
    if not message or not message.get("text", "").startswith("/"):
        return

    user = message.get("from", {})
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    if user.get("id") not in authorized_ids or chat.get("type") != "private":
        if chat_id is not None:
            client.send_message(chat_id, "This control panel is private.")
        return

    command = message["text"].split()[0].split("@")[0].lower()
    if command in {"/start", "/publish"}:
        client.send_message(
            chat_id,
            "Choose where to publish the next video:",
            destination_keyboard(),
        )


def process_callback(
    callback: dict[str, Any],
    client: TelegramClient,
    authorized_ids: set[int],
    handled_message_ids: set[tuple[int, int]],
) -> None:
    callback_id = callback["id"]
    user = callback.get("from", {})
    message = callback.get("message", {})
    chat = message.get("chat", {})
    chat_id = chat.get("id")

    if user.get("id") not in authorized_ids or chat.get("type") != "private":
        client.answer_callback_query(callback_id, "Not authorized")
        return

    message_id = message.get("message_id")
    selection_id = (chat_id, message_id)
    if selection_id in handled_message_ids:
        client.answer_callback_query(callback_id, "This selection was already used")
        return

    data = callback.get("data", "")
    if data.startswith("dest:"):
        destination = data.removeprefix("dest:")
        if destination not in DESTINATIONS:
            client.answer_callback_query(callback_id, "Unknown destination")
            return
        handled_message_ids.add(selection_id)
        client.answer_callback_query(callback_id)
        client.clear_inline_keyboard(chat_id, message_id)
        if destination == "telegram":
            run_upload(destination, None, chat_id, client)
        else:
            client.send_message(
                chat_id,
                f"Choose a YouTube slot for {DESTINATIONS[destination]}:",
                slot_keyboard(destination),
            )
        return

    if data.startswith("slot:"):
        parts = data.split(":", maxsplit=2)
        if len(parts) != 3:
            client.answer_callback_query(callback_id, "Invalid publishing selection")
            return
        _, slot, destination = parts
        if slot not in {"A", "B"} or destination not in {"youtube", "both"}:
            client.answer_callback_query(callback_id, "Invalid publishing selection")
            return
        handled_message_ids.add(selection_id)
        client.answer_callback_query(callback_id)
        client.clear_inline_keyboard(chat_id, message_id)
        run_upload(destination, slot, chat_id, client)
        return

    client.answer_callback_query(callback_id, "Unknown action")


def run_upload(
    destination: str,
    slot: str | None,
    chat_id: int,
    client: TelegramClient,
) -> None:
    client.send_message(
        chat_id,
        f"Starting upload to {DESTINATIONS[destination]}. This may take a while.",
    )
    command = [
        sys.executable,
        "upload_unlisted.py",
        "--destination",
        destination,
    ]
    if slot:
        command.extend(["--slot", slot])

    try:
        completed = subprocess.run(
            command,
            cwd=Path(__file__).resolve().parent,
            check=False,
        )
    except OSError as exc:
        logger.exception("Could not start the uploader")
        client.send_message(chat_id, f"Could not start uploader ({type(exc).__name__}).")
        return

    if completed.returncode == 0:
        client.send_message(chat_id, "Upload workflow completed.")
    else:
        logger.error("Uploader exited with status %d", completed.returncode)
        client.send_message(
            chat_id,
            f"Upload failed with exit status {completed.returncode}. Check the bot console.",
        )


def main() -> None:
    client = TelegramClient.from_env()
    authorized_ids = allowed_user_ids()
    logger.info("Telegram control bot started.")
    stale_updates = client.get_updates(-1, timeout=0)
    offset = max(
        (update["update_id"] + 1 for update in stale_updates),
        default=0,
    )
    if stale_updates:
        logger.info("Discarded %d queued update(s) from before startup.", len(stale_updates))
    handled_message_ids: set[tuple[int, int]] = set()
    while True:
        for update in client.get_updates(offset):
            offset = max(offset, update["update_id"] + 1)
            process_update(update, client, authorized_ids, handled_message_ids)


if __name__ == "__main__":
    main()
