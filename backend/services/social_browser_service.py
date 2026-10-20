from __future__ import annotations

import json
import logging
import threading
from contextlib import contextmanager
from typing import Iterator, Literal

from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.webdriver import WebDriver

from services.social_selenium_common import PROFILE_ROOT, create_driver

SocialPlatform = Literal["instagram", "facebook"]
_HOME_URLS = {
    "instagram": "https://www.instagram.com/",
    "facebook": "https://www.facebook.com/",
}
_LOGIN_COOKIES = {
    "instagram": "sessionid",
    "facebook": "c_user",
}
_STATE_FILE = PROFILE_ROOT / "session_state.json"
_LOCK = threading.RLock()
_DRIVERS: dict[str, WebDriver] = {}
logger = logging.getLogger(__name__)


def _read_state() -> dict:
    if not _STATE_FILE.exists():
        return {}
    with _STATE_FILE.open("r", encoding="utf-8") as file:
        state = json.load(file)
    if not isinstance(state, dict):
        raise ValueError("Social session state must be a JSON object")
    return state


def _write_state(state: dict) -> None:
    PROFILE_ROOT.mkdir(parents=True, exist_ok=True)
    temporary_path = _STATE_FILE.with_suffix(".tmp")
    with temporary_path.open("w", encoding="utf-8") as file:
        json.dump(state, file, indent=2)
        file.write("\n")
    temporary_path.replace(_STATE_FILE)


def _has_login_cookie(driver: WebDriver, platform: SocialPlatform) -> bool:
    names = {cookie["name"] for cookie in driver.get_cookies()}
    return _LOGIN_COOKIES[platform] in names


def _driver_is_open(driver: WebDriver) -> bool:
    try:
        driver.current_url
        return True
    except WebDriverException:
        return False


def _get_open_driver(platform: SocialPlatform) -> WebDriver | None:
    driver = _DRIVERS.get(platform)
    if driver is None:
        return None
    if _driver_is_open(driver):
        return driver
    try:
        driver.quit()
    except WebDriverException:
        logger.warning("Could not clean up the stopped %s browser", platform)
    finally:
        _DRIVERS.pop(platform, None)
    return None


def start_social_login(platform: SocialPlatform) -> dict:
    with _LOCK:
        driver = _get_open_driver(platform)
        if driver:
            driver.get(_HOME_URLS[platform])
            return get_social_login_status(platform)

        driver = create_driver(platform)
        driver.get(_HOME_URLS[platform])
        _DRIVERS[platform] = driver
        return get_social_login_status(platform)


def complete_social_login(platform: SocialPlatform) -> dict:
    with _LOCK:
        driver = _get_open_driver(platform)
        if not driver:
            raise ValueError("Open the login window before confirming sign-in")
        if not _has_login_cookie(driver, platform):
            raise ValueError(
                f"{platform.capitalize()} sign-in is not detected yet. "
                "Finish signing in and any security check in the open Chrome window."
            )

        state = _read_state()
        state[platform] = {"authenticated": True}
        _write_state(state)
        return get_social_login_status(platform)


def get_social_login_status(platform: SocialPlatform) -> dict:
    with _LOCK:
        state = _read_state()
        platform_state = state.get(platform) or {}
        driver = _get_open_driver(platform)
        return {
            "platform": platform,
            "authenticated": bool(platform_state.get("authenticated")),
            "browser_open": driver is not None,
            "profile_saved": (PROFILE_ROOT / platform).exists(),
        }


@contextmanager
def social_browser(platform: SocialPlatform) -> Iterator[WebDriver]:
    with _LOCK:
        driver = _get_open_driver(platform)
        if not driver:
            driver = create_driver(platform)
            _DRIVERS[platform] = driver
        driver.get(_HOME_URLS[platform])
        if not _has_login_cookie(driver, platform):
            state = _read_state()
            state[platform] = {"authenticated": False}
            _write_state(state)
            raise RuntimeError(
                f"{platform.capitalize()} is not signed in. Open Automation settings "
                "and complete the manual login first."
            )
        state = _read_state()
        state[platform] = {"authenticated": True}
        _write_state(state)
        yield driver


def close_social_browsers() -> None:
    with _LOCK:
        for platform, driver in list(_DRIVERS.items()):
            try:
                driver.quit()
            except WebDriverException:
                logger.warning("Could not close the %s browser cleanly", platform)
            finally:
                _DRIVERS.pop(platform, None)
