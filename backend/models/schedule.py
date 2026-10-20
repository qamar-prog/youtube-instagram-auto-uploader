from __future__ import annotations

from datetime import date, time
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, model_validator


PlatformName = Literal["youtube", "telegram", "instagram", "facebook"]


def is_facebook_page_url(value: str | None) -> bool:
    if not value:
        return False
    try:
        parsed = urlsplit(value.strip())
    except ValueError:
        return False
    hostname = (parsed.hostname or "").lower()
    return (
        parsed.scheme == "https"
        and (hostname == "facebook.com" or hostname.endswith(".facebook.com"))
        and bool(parsed.path.strip("/"))
        and parsed.username is None
        and parsed.password is None
    )


class PlatformSchedule(BaseModel):
    date: str | None = None
    time: str | None = None
    status: str = "scheduled"


class ScheduleCreate(BaseModel):
    video_id: str = Field(..., min_length=1)
    platforms: list[PlatformName] = Field(..., min_length=1)
    youtube: PlatformSchedule | None = None
    telegram: PlatformSchedule | None = None
    instagram: PlatformSchedule | None = None
    facebook: PlatformSchedule | None = None
    facebook_page_url: str | None = None
    timezone: str = "Asia/Karachi"

    @model_validator(mode="after")
    def validate_platform_selection(self):
        validate_schedule_configuration(
            self.platforms,
            {
                "youtube": self.youtube,
                "telegram": self.telegram,
                "instagram": self.instagram,
                "facebook": self.facebook,
            },
            self.timezone,
        )
        if "facebook" in self.platforms and not is_facebook_page_url(
            self.facebook_page_url
        ):
            raise ValueError("A Facebook Page URL is required for Facebook publishing")
        return self


class ScheduleUpdate(BaseModel):
    video_id: str | None = Field(default=None, min_length=1)
    platforms: list[PlatformName] | None = None
    youtube: PlatformSchedule | None = None
    telegram: PlatformSchedule | None = None
    instagram: PlatformSchedule | None = None
    facebook: PlatformSchedule | None = None
    facebook_page_url: str | None = None
    timezone: str | None = None
    status: str | None = None

    @model_validator(mode="after")
    def validate_platform_configuration(self):
        if self.platforms is not None and not self.platforms:
            raise ValueError("platforms cannot be empty")
        if self.platforms is not None:
            validate_schedule_configuration(
                self.platforms,
                {
                    "youtube": self.youtube,
                    "telegram": self.telegram,
                    "instagram": self.instagram,
                    "facebook": self.facebook,
                },
                self.timezone or "Asia/Karachi",
            )
            if "facebook" in self.platforms and not is_facebook_page_url(
                self.facebook_page_url
            ):
                raise ValueError("A Facebook Page URL is required for Facebook publishing")
        return self


def validate_schedule_configuration(
    platforms: list[PlatformName],
    schedules: dict[str, PlatformSchedule | None],
    timezone_name: str,
) -> None:
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"Unknown timezone: {timezone_name}") from exc

    for name in ("youtube", "telegram", "instagram", "facebook"):
        schedule = schedules.get(name)
        if name not in platforms:
            continue
        if schedule is None or not schedule.date or not schedule.time:
            raise ValueError(f"{name} requires both a date and a time")
        try:
            date.fromisoformat(schedule.date)
            time.fromisoformat(schedule.time)
        except ValueError as exc:
            raise ValueError(f"{name} date/time must be valid ISO values") from exc
