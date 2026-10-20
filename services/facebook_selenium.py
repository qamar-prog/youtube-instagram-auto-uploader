import time
from pathlib import Path

from selenium.webdriver.common.by import By
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    ElementNotInteractableException,
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
)
from selenium.webdriver.support.ui import WebDriverWait

from backend.models.schedule import is_facebook_page_url
from .social_selenium_common import create_driver, wait_for_manual_login

POST_READY_TIMEOUT = 180
PUBLISH_CONFIRMATION_TIMEOUT = 120


def _xpath_literal(value):
    if "'" not in value:
        return f"'{value}'"
    if '"' not in value:
        return f'"{value}"'
    parts = value.split("'")
    return "concat(" + ", \"'\", ".join(f"'{part}'" for part in parts) + ")"


def _find_text_control(driver, label):
    text = _xpath_literal(label)
    xpath = (
        "//*[self::button or @role='button' or @tabindex='0']"
        f"[normalize-space(.)={text} or .//*[normalize-space(.)={text}]]"
    )
    for element in driver.find_elements(By.XPATH, xpath):
        try:
            if (
                not element.is_displayed()
                or not element.is_enabled()
                or (element.get_attribute("disabled") is not None)
                or (element.get_attribute("aria-disabled") or "").lower() == "true"
            ):
                continue
            return element
        except (NoSuchElementException, StaleElementReferenceException):
            continue
    return False


def _click_text(driver, labels, timeout=12):
    last_error = None
    for label in labels:
        def click_visible_control(current_driver):
            nonlocal last_error
            element = _find_text_control(current_driver, label)
            if not element:
                return False
            try:
                element.click()
                return True
            except (
                ElementClickInterceptedException,
                ElementNotInteractableException,
                NoSuchElementException,
                StaleElementReferenceException,
            ) as exc:
                last_error = exc
                return False

        try:
            WebDriverWait(driver, timeout).until(click_visible_control)
            return
        except TimeoutException as exc:
            last_error = exc
    raise RuntimeError(
        f"Could not find a clickable Facebook control labelled {labels}. "
        "The UI may have changed or use another language."
    ) from last_error


def _click_post_when_ready(driver, timeout=POST_READY_TIMEOUT):
    try:
        post_button = WebDriverWait(driver, timeout).until(
            lambda current_driver: _find_text_control(current_driver, "Post")
        )
    except TimeoutException as exc:
        raise RuntimeError(
            "Facebook's Post button stayed disabled for "
            f"{timeout} seconds. The video may still be processing or Facebook "
            "requires another setting. The post was not submitted."
        ) from exc
    post_button.click()


def _wait_for_publish_confirmation(
    driver,
    timeout=PUBLISH_CONFIRMATION_TIMEOUT,
):
    def publish_confirmed(current_driver):
        message = (current_driver.find_element(By.TAG_NAME, "body").text or "").casefold()
        return any(
            phrase in message
            for phrase in (
                "your reel has been shared",
                "your post has been shared",
                "your reel was shared",
                "your post was shared",
                "your reel has been published",
                "your post has been published",
                "your reel is published",
                "your post is published",
                "posted successfully",
            )
        )

    try:
        WebDriverWait(driver, timeout).until(publish_confirmed)
    except TimeoutException as exc:
        raise RuntimeError(
            "Facebook did not show a publish confirmation within "
            f"{timeout} seconds. The result is unconfirmed; check the Page "
            "before retrying to avoid a duplicate post."
        ) from exc


def _open_post_composer(driver):
    _click_text(
        driver,
        ["Share a thought...", "Share a thought", "What's on your mind?", "Create post"],
    )


def _find_video_file_input(driver, timeout=20):
    def find_video_input(current_driver):
        for element in current_driver.find_elements(
            By.CSS_SELECTOR,
            "input[type='file']",
        ):
            accept = (element.get_attribute("accept") or "").lower()
            if "video/" in accept or any(
                extension in accept
                for extension in (".mp4", ".mov", ".m4v", ".webm", ".avi")
            ):
                return element
        return False

    try:
        return WebDriverWait(driver, timeout).until(find_video_input)
    except TimeoutException as exc:
        raise RuntimeError(
            "Facebook's post composer did not expose a video upload input. "
            "The scheduled video was not selected; no system file picker was opened."
        ) from exc


def _wait_for_stage(driver, stage, timeout=30):
    expected_text = stage.casefold()

    def stage_is_visible(current_driver):
        body = current_driver.find_element(By.TAG_NAME, "body")
        return expected_text in body.text.casefold()

    try:
        WebDriverWait(driver, timeout).until(stage_is_visible)
    except TimeoutException as exc:
        raise RuntimeError(
            f"Facebook did not open the expected '{stage}' step after video selection."
        ) from exc


def _find_reel_description_box(driver, timeout=30):
    def find_description_box(current_driver):
        selectors = (
            "div[contenteditable='true'],"
            "textarea,"
            "[role='textbox']"
        )
        for element in current_driver.find_elements(By.CSS_SELECTOR, selectors):
            if not element.is_displayed() or not element.is_enabled():
                continue
            identifying_text = " ".join(
                element.get_attribute(attribute) or ""
                for attribute in ("placeholder", "aria-label", "data-placeholder")
            ).casefold()
            if "describe your reel" in identifying_text or "reel description" in identifying_text:
                return element
        return False

    try:
        return WebDriverWait(driver, timeout).until(find_description_box)
    except TimeoutException as exc:
        raise RuntimeError(
            "Facebook opened Reel settings but its 'Describe your reel' field "
            "was not found; the caption was not entered and the post was not submitted."
        ) from exc


def upload_facebook(
    video_path: str,
    caption: str,
    page_url: str,
    publish: bool = False,
    driver=None,
):
    """
    Best-effort Facebook Page browser upload.
    Start with publish=False to prepare and inspect the post without clicking Post.
    """
    path = Path(video_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Video not found: {path}")
    if not is_facebook_page_url(page_url):
        raise ValueError("Provide the full URL of the Facebook Page you manage.")

    owns_driver = driver is None
    driver = driver or create_driver("facebook")
    try:
        if owns_driver:
            wait_for_manual_login(driver, "Facebook", "https://www.facebook.com/")
        driver.get(page_url)
        time.sleep(4)

        _open_post_composer(driver)
        file_input = _find_video_file_input(driver)
        file_input.send_keys(str(path))

        _click_text(driver, ["Next"])
        _wait_for_stage(driver, "Edit reel")
        _click_text(driver, ["Next"])
        _wait_for_stage(driver, "Reel settings")
        caption_box = _find_reel_description_box(driver)
        caption_box.click()
        caption_box.send_keys(caption)

        if publish:
            _click_post_when_ready(driver)
            _wait_for_publish_confirmation(driver)
        return True
    finally:
        if publish and owns_driver:
            driver.quit()
