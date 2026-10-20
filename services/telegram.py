"""Small Telegram Bot API client for video delivery and bot control."""

import json
import mimetypes
import os
from pathlib import Path
from typing import Any

import requests


class TelegramClient:
    def __init__(self, token: str, timeout: int = 300) -> None:
        token = token.strip()
        if not token:
            raise ValueError("TELEGRAM_BOT_TOKEN is not configured")
        self._base_url = f"https://api.telegram.org/bot{token}"
        self._timeout = timeout

    @classmethod
    def from_env(cls) -> "TelegramClient":
        return cls(os.getenv("TELEGRAM_BOT_TOKEN", ""))

    def send_video(self, video_path: str, caption: str) -> int:
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        if not chat_id:
            raise ValueError("TELEGRAM_CHAT_ID is not configured")

        mime_type = mimetypes.guess_type(video_path)[0] or "application/octet-stream"
        with open(video_path, "rb") as video_file:
            result = self._call(
                "sendVideo",
                data={
                    "chat_id": chat_id,
                    "caption": build_video_caption("", caption),
                    "supports_streaming": "true",
                },
                files={
                    "video": (Path(video_path).name, video_file, mime_type),
                },
            )
        return int(result["message_id"])

    def send_message(
        self, chat_id: int, text: str, reply_markup: dict[str, Any] | None = None
    ) -> None:
        data: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            data["reply_markup"] = reply_markup
        self._call("sendMessage", data=data)

    def answer_callback_query(self, callback_query_id: str, text: str = "") -> None:
        self._call(
            "answerCallbackQuery",
            data={"callback_query_id": callback_query_id, "text": text},
        )

    def clear_inline_keyboard(self, chat_id: int, message_id: int) -> None:
        self._call(
            "editMessageReplyMarkup",
            data={
                "chat_id": chat_id,
                "message_id": message_id,
                "reply_markup": {"inline_keyboard": []},
            },
        )

    def get_updates(self, offset: int, timeout: int = 30) -> list[dict[str, Any]]:
        result = self._call(
            "getUpdates",
            data={
                "offset": offset,
                "timeout": timeout,
                "allowed_updates": ["message", "callback_query"],
            },
            timeout=timeout + 5,
        )
        if not isinstance(result, list):
            raise RuntimeError("Telegram getUpdates returned an invalid result")
        return result

    def _call(
        self,
        method: str,
        data: dict[str, Any],
        files: dict[str, Any] | None = None,
        timeout: int | None = None,
    ) -> Any:
        try:
            response = requests.post(
                f"{self._base_url}/{method}",
                data={
                    key: json.dumps(value) if isinstance(value, (dict, list)) else value
                    for key, value in data.items()
                },
                files=files,
                timeout=self._timeout if timeout is None else timeout,
            )
        except requests.RequestException as exc:
            raise RuntimeError(
                f"Telegram {method} request failed ({type(exc).__name__})"
            ) from None

        try:
            payload = response.json()
        except ValueError:
            raise RuntimeError(
                f"Telegram {method} returned HTTP {response.status_code} with invalid JSON"
            ) from None

        if not isinstance(payload, dict):
            raise RuntimeError(f"Telegram {method} returned an invalid response")
        if not response.ok or not payload.get("ok"):
            description = payload.get("description", f"HTTP {response.status_code}")
            raise RuntimeError(f"Telegram {method} failed: {description}")
        return payload.get("result")


def build_video_caption(title: str, description: str) -> str:
    """Build a plain-text Telegram caption within the Bot API limit."""
    parts = [part.strip() for part in (title, description) if part.strip()]
    caption = "\n\n".join(parts)
    if _utf16_length(caption) > 1024:
        truncated = []
        length = 0
        for character in caption:
            character_length = 2 if ord(character) > 0xFFFF else 1
            if length + character_length > 1021:
                break
            truncated.append(character)
            length += character_length
        caption = "".join(truncated).rstrip() + "..."
    return caption


def _utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2
