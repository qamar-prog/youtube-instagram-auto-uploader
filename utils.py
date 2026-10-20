"""
Shared utility helpers used across the pipeline.

  - load_json / save_json  — safe file I/O with atomic-ish writes
  - retry_on_transient     — decorator for exponential-backoff retries on
                             HTTP 429 / 5xx responses from requests.Response
"""

import json
import os
import time
import functools
import logging
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import requests

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# JSON state helpers
# ---------------------------------------------------------------------------

def load_json(path: str, default: Any) -> Any:
    """Return parsed JSON from *path*, or *default* if the file doesn't exist."""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return default


def save_json(path: str, data: Any) -> None:
    """Atomically write *data* as pretty-printed JSON to *path*.

    Writes to a temp file first, then renames so a crash mid-write never
    leaves a truncated file behind.
    """
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)  # atomic on POSIX; best-effort on Windows


# ---------------------------------------------------------------------------
# Retry decorator
# ---------------------------------------------------------------------------

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def _rate_limit_delay(exc: requests.HTTPError) -> float | None:
    response = exc.response
    if response is None:
        return None

    headers = response.headers or {}
    retry_after = headers.get("Retry-After")
    if retry_after:
        try:
            return max(0.0, float(retry_after))
        except ValueError:
            duration = _duration_seconds(retry_after)
            if duration is not None:
                return duration
            try:
                retry_at = parsedate_to_datetime(retry_after)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                pass

    reset_delays = []
    for name, value in headers.items():
        if name.lower().startswith("x-ratelimit-reset") and value:
            seconds = _duration_seconds(str(value))
            if seconds is not None:
                reset_delays.append(seconds)
    if reset_delays:
        return max(reset_delays)

    try:
        payload = response.json()
    except (ValueError, requests.JSONDecodeError):
        payload = {}
    if isinstance(payload, dict):
        retry_after_value = payload.get("retry_after")
        if isinstance(retry_after_value, (int, float)):
            return max(0.0, float(retry_after_value))
        error = payload.get("error")
        message = error.get("message", "") if isinstance(error, dict) else ""
        if message:
            match = re.search(
                r"(?:try again|reset|retry)(?: in| after)\s+([0-9a-z. ]+)",
                message,
                flags=re.IGNORECASE,
            )
            if match:
                return _duration_seconds(match.group(1))
    return None


def _duration_seconds(value: str) -> float | None:
    normalized = value.strip().lower()
    try:
        return max(0.0, float(normalized))
    except ValueError:
        pass

    compact = re.sub(r"\s+", "", normalized)
    matches = list(
        re.finditer(r"(\d+(?:\.\d+)?)(ms|sec|s|min|m|hr|h|d)", compact)
    )
    if not matches or "".join(match.group(0) for match in matches) != compact:
        return None
    factors = {
        "ms": 0.001,
        "s": 1.0,
        "sec": 1.0,
        "m": 60.0,
        "min": 60.0,
        "h": 3600.0,
        "hr": 3600.0,
        "d": 86400.0,
    }
    return sum(
        float(match.group(1)) * factors[match.group(2)]
        for match in matches
    )


def retry_on_transient(
    max_attempts: int = 4,
    base_delay: float = 2.0,
    backoff: float = 2.0,
    max_retry_delay: float | None = None,
):
    """Decorator: retry the wrapped function on transient HTTP / network errors.

    The wrapped function must raise ``requests.HTTPError`` (from
    ``response.raise_for_status()``) or ``requests.ConnectionError`` /
    ``requests.Timeout`` for the retry logic to kick in.

    Args:
        max_attempts: total attempts (including the first).
        base_delay: seconds to wait before the second attempt.
        backoff: multiplier applied to the delay after each failure.
        max_retry_delay: maximum server-directed delay before raising the
                         original HTTP error so a fallback can be used.

    Raises:
        The last exception if all attempts are exhausted.
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            delay = base_delay
            last_exc: Exception | None = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except requests.HTTPError as exc:
                    status = exc.response.status_code if exc.response is not None else None
                    if status not in _RETRYABLE_STATUS:
                        raise  # non-retryable (4xx client errors, etc.)
                    last_exc = exc
                    retry_delay = (
                        _rate_limit_delay(exc)
                        if status == 429
                        else None
                    )
                    if (
                        retry_delay is not None
                        and max_retry_delay is not None
                        and retry_delay > max_retry_delay
                    ):
                        logger.warning(
                            "%s: HTTP 429 reset is %.1fs away; skipping early retry "
                            "and allowing a configured fallback",
                            func.__name__,
                            retry_delay,
                        )
                        raise
                    if retry_delay is not None:
                        delay = retry_delay
                    logger.warning(
                        "%s: HTTP %s on attempt %d/%d — retrying in %.1fs",
                        func.__name__, status, attempt, max_attempts, delay,
                    )
                except (requests.ConnectionError, requests.Timeout) as exc:
                    last_exc = exc
                    logger.warning(
                        "%s: network error on attempt %d/%d — retrying in %.1fs: %s",
                        func.__name__, attempt, max_attempts, delay, exc,
                    )

                if attempt < max_attempts:
                    time.sleep(delay)
                    delay *= backoff

            raise last_exc  # type: ignore[misc]

        return wrapper
    return decorator
