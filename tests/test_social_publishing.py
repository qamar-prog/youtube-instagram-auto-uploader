import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, call, patch

from pydantic import ValidationError
from selenium.common.exceptions import (
    SessionNotCreatedException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)

from backend.models.schedule import PlatformSchedule, ScheduleCreate
from backend.services import drive_automation_service, publishing_worker
from backend.services import scheduler_service, social_browser_service
from services import facebook_selenium, instagram_selenium, social_selenium_common


class SocialScheduleTests(unittest.TestCase):
    def test_facebook_schedule_requires_managed_page_url(self):
        date = "2030-01-01"
        with self.assertRaises(ValidationError):
            ScheduleCreate(
                video_id="drive-video",
                platforms=["facebook"],
                facebook=PlatformSchedule(date=date, time="12:00"),
                timezone="UTC",
            )

        payload = ScheduleCreate(
            video_id="drive-video",
            platforms=["instagram", "facebook"],
            instagram=PlatformSchedule(date=date, time="12:00"),
            facebook=PlatformSchedule(date=date, time="12:30"),
            facebook_page_url="https://www.facebook.com/my-page",
            timezone="UTC",
        )
        self.assertEqual(payload.platforms, ["instagram", "facebook"])

    def test_facebook_web_subdomain_page_url_is_accepted(self):
        payload = ScheduleCreate(
            video_id="drive-video",
            platforms=["facebook"],
            facebook=PlatformSchedule(date="2030-01-01", time="12:00"),
            facebook_page_url="https://web.facebook.com/my-page",
            timezone="UTC",
        )

        self.assertEqual(
            payload.facebook_page_url,
            "https://web.facebook.com/my-page",
        )

    def test_facebook_page_url_rejects_non_facebook_hosts(self):
        for page_url in (
            "http://www.facebook.com/my-page",
            "https://facebook.com.evil.example/my-page",
            "https://example.com/my-page",
        ):
            with self.subTest(page_url=page_url), self.assertRaises(ValidationError):
                drive_automation_service.DriveAutomationSettings(
                    enabled=True,
                    platforms=["facebook"],
                    facebook_page_url=page_url,
                )

    def test_drive_auto_schedules_selected_social_destinations_from_drive_id(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings_path = root / "drive_automation_settings.json"
            state_path = root / "drive_automation_state.json"
            queue_path = root / "publish_queue.json"
            queue_path.write_text("[]", encoding="utf-8")
            settings = drive_automation_service.DriveAutomationSettings(
                enabled=True,
                interval_minutes=30,
                platforms=["instagram", "facebook"],
                facebook_page_url="https://www.facebook.com/my-page",
                timezone="UTC",
            )
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
                patch(
                    "backend.services.social_browser_service.get_social_login_status",
                    return_value={"authenticated": True},
                ),
            ):
                drive_automation_service.save_drive_automation_settings(settings)
                captured = []
                with (
                    patch.object(
                        drive_automation_service,
                        "list_drive_videos",
                        return_value=[
                            {
                                "drive_file_id": "drive-video",
                                "filename": "from-drive.mp4",
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
                        side_effect=lambda payload: captured.append(payload),
                    ),
                ):
                    count = drive_automation_service.process_new_drive_videos(
                        datetime(2030, 1, 1, 10, 0, tzinfo=timezone.utc)
                    )

        self.assertEqual(count, 1)
        self.assertEqual(captured[0].video_id, "drive-video")
        self.assertEqual(captured[0].platforms, ["instagram", "facebook"])
        self.assertEqual(captured[0].instagram.time, "10:30")
        self.assertEqual(captured[0].facebook.time, "10:30")
        self.assertEqual(
            captured[0].facebook_page_url, "https://www.facebook.com/my-page"
        )

    def test_drive_auto_settings_require_social_login_before_enabling(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch.object(
                    drive_automation_service,
                    "DRIVE_AUTOMATION_SETTINGS_FILE",
                    root / "settings.json",
                ),
                patch.object(
                    drive_automation_service,
                    "DRIVE_AUTOMATION_STATE_FILE",
                    root / "state.json",
                ),
                patch(
                    "backend.services.social_browser_service.get_social_login_status",
                    return_value={"authenticated": False},
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "instagram"):
                    drive_automation_service.save_drive_automation_settings(
                        drive_automation_service.DriveAutomationSettings(
                            enabled=True,
                            platforms=["instagram"],
                        )
                    )

            self.assertFalse((root / "settings.json").exists())

    def test_manual_login_session_saves_profile_state_without_passwords(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            driver = Mock()
            driver.get_cookies.return_value = [{"name": "sessionid"}]
            state_file = root / "session_state.json"
            with (
                patch.object(social_browser_service, "_STATE_FILE", state_file),
                patch.object(social_browser_service, "PROFILE_ROOT", root),
                patch.dict(social_browser_service._DRIVERS, {"instagram": driver}, clear=True),
            ):
                result = social_browser_service.complete_social_login("instagram")
                saved = json.loads(state_file.read_text(encoding="utf-8"))

        self.assertTrue(result["authenticated"])
        self.assertEqual(saved, {"instagram": {"authenticated": True}})


class SocialPublishingStatusTests(unittest.TestCase):
    def test_schedule_completes_after_social_platforms_publish(self):
        with tempfile.TemporaryDirectory() as directory:
            queue_path = Path(directory) / "publish_queue.json"
            queue_path.write_text(
                json.dumps(
                    [
                        {
                            "schedule_id": "social-post",
                            "video_id": "drive-video",
                            "platforms": ["instagram", "facebook"],
                            "instagram": {"status": "scheduled"},
                            "facebook": {"status": "scheduled"},
                            "status": "scheduled",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            with (
                patch.object(scheduler_service, "QUEUE_FILE", queue_path),
                patch.object(publishing_worker, "_record_processed_video") as record,
            ):
                publishing_worker._set_platform_status(
                    "social-post", "instagram", "published"
                )
                result = publishing_worker._set_platform_status(
                    "social-post", "facebook", "published"
                )

        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["published"])
        record.assert_called_once_with("drive-video")


class ManagedBrowserUploadTests(unittest.TestCase):
    def test_open_composer_clicks_create_and_post_after_dom_verification(self):
        driver = Mock()
        snapshots = iter(
            [
                {"state": "normal_instagram_page"},
                {
                    "state": "dialog_open",
                    "has_dialog": True,
                    "composer_post_option_found": True,
                    "has_upload_picker": False,
                },
                {
                    "state": "dialog_open",
                    "has_dialog": True,
                    "has_upload_picker": True,
                },
            ]
        )
        wait = Mock()
        wait.until.side_effect = lambda predicate: predicate(driver)
        with (
            patch.object(instagram_selenium, "_inspect_page", side_effect=lambda _: next(snapshots)),
            patch.object(instagram_selenium, "_click_inspected_control") as click_control,
            patch.object(instagram_selenium, "WebDriverWait", return_value=wait),
        ):
            instagram_selenium._open_post_composer(driver)

        self.assertEqual(
            click_control.call_args_list,
            [
                call(driver, "create", timeout=8),
                call(driver, "post", timeout=5),
            ],
        )

    def test_open_composer_accepts_upload_picker_without_post_option(self):
        driver = Mock()
        snapshots = iter(
            [
                {"state": "normal_instagram_page"},
                {
                    "state": "dialog_open",
                    "has_dialog": True,
                    "composer_post_option_found": False,
                    "has_upload_picker": True,
                },
            ]
        )
        wait = Mock()
        wait.until.side_effect = lambda predicate: predicate(driver)
        with (
            patch.object(instagram_selenium, "_inspect_page", side_effect=lambda _: next(snapshots)),
            patch.object(instagram_selenium, "_click_inspected_control") as click_control,
            patch.object(instagram_selenium, "WebDriverWait", return_value=wait),
        ):
            instagram_selenium._open_post_composer(driver)

        click_control.assert_called_once_with(driver, "create", timeout=8)

    def test_create_without_menu_returns_clear_error(self):
        wait = Mock()
        wait.until.side_effect = TimeoutException()
        with (
            patch.object(instagram_selenium, "_inspect_page", side_effect=[
                {"state": "normal_instagram_page"},
                {"state": "normal_instagram_page", "has_dialog": False},
            ]),
            patch.object(instagram_selenium, "_click_inspected_control"),
            patch.object(instagram_selenium, "WebDriverWait", return_value=wait),
            self.assertRaisesRegex(RuntimeError, "Post menu did not appear"),
        ):
            instagram_selenium._open_post_composer(Mock())

    def test_open_post_composer_stops_for_login_page(self):
        with (
            patch.object(
                instagram_selenium,
                "_inspect_page",
                return_value={"state": "logged_out"},
            ),
            patch.object(instagram_selenium, "_click_inspected_control") as click_control,
            self.assertRaisesRegex(RuntimeError, "login page"),
        ):
            instagram_selenium._open_post_composer(Mock())
        click_control.assert_not_called()

    def test_missing_create_returns_clear_error_without_writing_files(self):
        driver = Mock()
        snapshot = {
            "state": "normal_instagram_page",
            "route_kind": "home",
            "has_dialog": False,
            "has_navigation": True,
            "create_control_found": False,
            "candidates": [
                {
                    "scope": "page",
                    "tag": "a",
                    "role": "link",
                    "aria_label": "",
                    "title": "",
                    "icon_label": "home",
                    "data_testid": "",
                    "labels": ["create"],
                    "href_kind": "hash",
                }
            ],
        }
        wait = Mock()
        wait.until.side_effect = TimeoutException()
        with (
            patch.object(instagram_selenium, "_inspect_page", return_value=snapshot),
            patch.object(instagram_selenium, "WebDriverWait", return_value=wait),
            self.assertRaisesRegex(RuntimeError, "was not found on page state"),
        ):
            instagram_selenium._click_control(driver, ["Create"], timeout=1)
        driver.save_screenshot.assert_not_called()

    def test_create_control_must_be_in_navigation_not_a_profile_link(self):
        driver = Mock()
        driver.execute_script.return_value = {"candidates": [
            {
                "scope": "page",
                "tag": "a",
                "role": "",
                "aria_label": "",
                "title": "",
                "data_testid": "",
                "labels": ["create"],
            }
        ]}
        self.assertIsNone(instagram_selenium._find_control(driver, "create"))

    def test_create_icon_link_outside_navigation_is_rejected(self):
        driver = Mock()
        driver.execute_script.return_value = {
            "candidates": [
                {
                    "scope": "page",
                    "tag": "a",
                    "role": "link",
                    "aria_label": "",
                    "title": "",
                    "icon_label": "new post",
                    "data_testid": "",
                    "labels": ["new post", "create"],
                    "href_kind": "hash",
                }
            ]
        }

        result = instagram_selenium._find_control(driver, "create")

        self.assertIsNone(result)

    def test_create_icon_link_uses_home_page_fallback_when_nav_landmark_omits_it(self):
        element = Mock()
        element.tag_name = "a"
        element.is_displayed.return_value = True
        element.is_enabled.return_value = True
        element.get_attribute.side_effect = lambda attribute: (
            "link" if attribute == "role" else "#" if attribute == "href" else None
        )
        icon = Mock()
        icon.get_attribute.side_effect = lambda attribute: (
            "new post" if attribute == "aria-label" else None
        )
        element.find_elements.return_value = [icon]
        element.text = ""
        driver = Mock()
        driver.execute_script.side_effect = [
            {
                "route_kind": "home",
                "has_navigation": True,
                "candidates": [
                    {
                        "scope": "page",
                        "tag": "a",
                        "role": "link",
                        "aria_label": "",
                        "title": "",
                        "icon_label": "new post",
                        "data_testid": "",
                        "labels": ["new post", "create"],
                        "href_kind": "hash",
                    }
                ],
            },
            element,
        ]

        result = instagram_selenium._find_control(driver, "create")

        self.assertIs(result, element)
        self.assertEqual(driver.execute_script.call_count, 2)

    def test_create_selector_requires_live_dom_match_visible_and_enabled(self):
        snapshot = {
            "route_kind": "home",
            "has_navigation": True,
            "candidates": [
                {
                    "scope": "page",
                    "tag": "a",
                    "role": "link",
                    "icon_label": "new post",
                    "href_kind": "hash",
                    "labels": ["create"],
                }
            ],
        }
        for visible, enabled in ((False, True), (True, False)):
            with self.subTest(visible=visible, enabled=enabled):
                element = Mock()
                element.is_displayed.return_value = visible
                element.is_enabled.return_value = enabled
                driver = Mock()
                driver.execute_script.side_effect = [snapshot, element]

                self.assertIsNone(instagram_selenium._find_control(driver, "create"))

    def test_next_uses_exact_enabled_button_inside_active_composer(self):
        element = Mock()
        element.is_displayed.return_value = True
        element.is_enabled.return_value = True
        driver = Mock()
        driver.execute_script.side_effect = [
            {
                "candidates": [
                    {
                        "scope": "composer",
                        "tag": "div",
                        "role": "button",
                        "labels": ["next"],
                    }
                ]
            },
            element,
        ]

        result = instagram_selenium._find_control(driver, "next")

        self.assertIs(result, element)
        self.assertEqual(driver.execute_script.call_count, 2)

    def test_share_can_match_observed_clickable_without_button_role(self):
        element = Mock()
        element.is_displayed.return_value = True
        element.is_enabled.return_value = True
        driver = Mock()
        driver.execute_script.side_effect = [
            {"candidates": [], "has_dialog": True},
            element,
        ]

        result = instagram_selenium._find_control(driver, "share")

        self.assertIs(result, element)
        selector_script = driver.execute_script.call_args.args[0]
        self.assertIn('getComputedStyle(element).cursor === "pointer"', selector_script)
        self.assertIn('label(element) === "share"', selector_script)

    def test_share_completion_waits_for_sharing_indicator_to_disappear(self):
        sharing = {
            "sharing": True,
            "success": False,
            "failure": False,
            "composer_open": False,
            "navigation": True,
        }
        finished = {
            "sharing": False,
            "success": False,
            "failure": False,
            "composer_open": False,
            "navigation": True,
        }
        driver = Mock()
        driver.execute_script.side_effect = [sharing, sharing, finished, finished, finished]
        wait = Mock()

        def poll_until_complete(predicate):
            for _ in range(6):
                if predicate(driver):
                    return True
            raise TimeoutException()

        wait.until.side_effect = poll_until_complete
        with patch.object(
            instagram_selenium, "WebDriverWait", return_value=wait
        ) as wait_factory:
            instagram_selenium._wait_for_share_completion(driver, timeout=20)

        self.assertEqual(driver.execute_script.call_count, 5)
        wait_factory.assert_called_once_with(driver, 20)

    def test_share_completion_timeout_is_reported_as_unconfirmed(self):
        wait = Mock()
        wait.until.side_effect = TimeoutException()

        with (
            patch.object(instagram_selenium, "WebDriverWait", return_value=wait),
            self.assertRaisesRegex(RuntimeError, "result is unconfirmed"),
        ):
            instagram_selenium._wait_for_share_completion(Mock(), timeout=10)

        self.assertEqual(wait.until.call_count, 1)

    def test_share_completion_surfaces_instagram_upload_errors(self):
        driver = Mock()
        driver.execute_script.return_value = {
            "sharing": False,
            "success": False,
            "failure": True,
            "failure_message": "Post couldn't be shared. Your post could not be shared. Please try again.",
            "composer_open": True,
            "navigation": True,
        }
        wait = Mock()
        wait.until.side_effect = lambda predicate: predicate(driver)

        with (
            patch.object(instagram_selenium, "WebDriverWait", return_value=wait),
            self.assertRaisesRegex(
                RuntimeError,
                "Post couldn't be shared.*Please try again",
            ),
        ):
            instagram_selenium._wait_for_share_completion(driver)

    def test_next_click_retries_if_react_replaces_the_element(self):
        stale_element = Mock()
        stale_element.click.side_effect = StaleElementReferenceException()
        current_element = Mock()
        wait = Mock()
        driver = Mock()

        def run_until_click(predicate):
            for _ in range(3):
                result = predicate(driver)
                if result:
                    return result
            raise TimeoutException()

        wait.until.side_effect = run_until_click
        with (
            patch.object(
                instagram_selenium,
                "_inspect_page",
                return_value={"state": "dialog_open"},
            ),
            patch.object(
                instagram_selenium,
                "_find_control",
                side_effect=[stale_element, current_element],
            ),
            patch.object(instagram_selenium, "WebDriverWait", return_value=wait),
        ):
            instagram_selenium._click_control(driver, ["Next"], timeout=1)

        stale_element.click.assert_called_once_with()
        current_element.click.assert_called_once_with()

    def test_post_menu_option_is_scoped_to_create_menu_and_exact_text(self):
        post = Mock()
        post.is_displayed.return_value = True
        post.is_enabled.return_value = True
        driver = Mock()
        driver.execute_script.side_effect = [
            {
                "candidates": [
                    {
                        "scope": "composer",
                        "tag": "a",
                        "role": "link",
                        "labels": ["post"],
                        "href_kind": "hash",
                    }
                ]
            },
            post,
        ]

        result = instagram_selenium._find_control(driver, "post")

        self.assertIs(result, post)

    def test_create_control_uses_observed_navigation_icon_link(self):
        element = Mock()
        element.tag_name = "a"
        element.is_displayed.return_value = True
        element.is_enabled.return_value = True
        element.get_attribute.side_effect = lambda attribute: (
            "link" if attribute == "role" else "#" if attribute == "href" else None
        )
        icon = Mock()
        icon.get_attribute.side_effect = lambda attribute: (
            "new post" if attribute == "aria-label" else None
        )
        element.find_elements.return_value = [icon]
        element.text = ""
        driver = Mock()
        driver.execute_script.side_effect = [
            {
                "candidates": [
                    {
                        "scope": "navigation",
                        "tag": "a",
                        "role": "link",
                        "aria_label": "",
                        "title": "",
                        "icon_label": "new post",
                        "data_testid": "",
                        "labels": ["new post", "create"],
                        "href_kind": "hash",
                    }
                ]
            },
            element,
        ]

        result = instagram_selenium._find_control(driver, "create")

        self.assertIs(result, element)
        self.assertEqual(driver.execute_script.call_count, 2)

    def test_create_control_rejects_unrelated_test_id_button(self):
        element = Mock()
        element.tag_name = "button"
        element.is_displayed.return_value = True
        element.is_enabled.return_value = True
        element.get_attribute.side_effect = lambda attribute: (
            "topnav_create" if attribute == "data-testid" else None
        )
        element.find_elements.return_value = []
        element.text = ""
        driver = Mock()
        driver.execute_script.side_effect = [
            {
                "candidates": [
                    {
                        "scope": "navigation",
                        "tag": "button",
                        "role": "button",
                        "aria_label": "",
                        "title": "",
                        "icon_label": "",
                        "data_testid": "topnav_create",
                        "labels": ["create"],
                        "href_kind": "",
                    }
                ]
            }
        ]

        result = instagram_selenium._find_control(driver, "create")

        self.assertIsNone(result)

    def test_chrome_profile_start_failure_has_recovery_guidance(self):
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(social_selenium_common, "PROFILE_ROOT", Path(directory)),
                patch.object(
                    social_selenium_common.webdriver,
                    "Chrome",
                    side_effect=SessionNotCreatedException("DevToolsActivePort missing"),
                ),
                self.assertRaisesRegex(
                    RuntimeError,
                    "Close any Chrome window opened by this app",
                ),
            ):
                social_selenium_common.create_driver("instagram")

    def test_stopped_managed_chrome_driver_is_quit_and_removed(self):
        class StoppedDriver:
            @property
            def current_url(self):
                raise WebDriverException("browser stopped")

            def quit(self):
                self.quit_called = True

        driver = StoppedDriver()
        driver.quit_called = False
        social_browser_service._DRIVERS["instagram"] = driver
        try:
            result = social_browser_service._get_open_driver("instagram")
        finally:
            social_browser_service._DRIVERS.pop("instagram", None)

        self.assertIsNone(result)
        self.assertTrue(driver.quit_called)

    def test_social_browser_reuses_one_driver_for_repeated_upload_sessions(self):
        driver = Mock()
        driver.current_url = "https://www.instagram.com/"
        driver.get_cookies.return_value = [{"name": "sessionid"}]

        with (
            patch.dict(social_browser_service._DRIVERS, {}, clear=True),
            patch.object(social_browser_service, "create_driver", return_value=driver) as create,
            patch.object(social_browser_service, "_read_state", return_value={}),
            patch.object(social_browser_service, "_write_state"),
        ):
            with social_browser_service.social_browser("instagram") as first:
                self.assertIs(first, driver)
            with social_browser_service.social_browser("instagram") as second:
                self.assertIs(second, driver)

            create.assert_called_once_with("instagram")
            driver.quit.assert_not_called()

    def test_instagram_file_picker_opens_select_from_computer_when_needed(self):
        file_input = Mock()
        driver = Mock()
        wait = Mock()
        wait.until.side_effect = [TimeoutException(), file_input]
        with (
            patch.object(instagram_selenium, "WebDriverWait", return_value=wait),
            patch.object(instagram_selenium, "_click_control") as click_control,
        ):
            result = instagram_selenium._find_video_file_input(driver)

        self.assertIs(result, file_input)
        click_control.assert_called_once_with(
            driver,
            [
                "Select from computer",
                "Select from your computer",
                "Select files",
            ],
            timeout=4,
            diagnose_missing=False,
        )

    def test_instagram_file_picker_failure_explains_manual_check(self):
        wait = Mock()
        wait.until.side_effect = [TimeoutException(), TimeoutException()]
        driver = Mock()
        snapshot = {
            "state": "normal_instagram_page",
            "route_kind": "home",
            "candidates": [],
            "has_dialog": False,
            "has_navigation": True,
            "create_control_found": False,
        }
        with (
            patch.object(instagram_selenium, "WebDriverWait", return_value=wait),
            patch.object(instagram_selenium, "_inspect_page", return_value=snapshot),
            patch.object(instagram_selenium, "_click_control"),
            self.assertRaisesRegex(RuntimeError, "video upload input did not appear"),
        ):
            instagram_selenium._find_video_file_input(driver)

    def test_managed_instagram_upload_does_not_block_for_terminal_login(self):
        with tempfile.TemporaryDirectory() as directory:
            video_path = Path(directory) / "video.mp4"
            video_path.write_bytes(b"video")
            wait = Mock()
            wait.until.side_effect = [Mock(), Mock()]
            with (
                patch.object(instagram_selenium, "wait_for_manual_login") as manual_login,
                patch.object(instagram_selenium, "_open_post_composer"),
                patch.object(instagram_selenium, "_find_video_file_input", return_value=Mock()),
                patch.object(instagram_selenium, "_find_caption_box", return_value=Mock()),
                patch.object(instagram_selenium, "_wait_for_composer_stage"),
                patch.object(instagram_selenium, "_click_control"),
                patch.object(instagram_selenium.time, "sleep"),
            ):
                result = instagram_selenium.upload_instagram(
                    str(video_path),
                    "caption",
                    driver=Mock(),
                )

        self.assertTrue(result)
        manual_login.assert_not_called()

    def test_instagram_preview_advances_two_stages_then_enters_caption_without_sharing(self):
        events = []
        file_input = Mock()
        file_input.send_keys.side_effect = lambda _: events.append("video")
        caption_box = Mock()
        caption_box.click.side_effect = lambda: events.append("caption-focus")
        caption_box.send_keys.side_effect = lambda _: events.append("caption")
        driver = Mock()

        def record_stage(_driver, heading, **_kwargs):
            events.append(f"stage:{heading}")

        def record_click(_driver, labels, **_kwargs):
            events.append(f"click:{labels[0]}")

        with tempfile.TemporaryDirectory() as directory:
            video_path = Path(directory) / "video.mp4"
            video_path.write_bytes(b"video")
            with (
                patch.object(instagram_selenium, "_open_post_composer"),
                patch.object(instagram_selenium, "_find_video_file_input", return_value=file_input),
                patch.object(instagram_selenium, "_find_caption_box", return_value=caption_box),
                patch.object(instagram_selenium, "_wait_for_composer_stage", side_effect=record_stage),
                patch.object(instagram_selenium, "_click_control", side_effect=record_click),
                patch.object(instagram_selenium.time, "sleep"),
            ):
                result = instagram_selenium.upload_instagram(
                    str(video_path),
                    "test caption",
                    publish=False,
                    driver=driver,
                )

        self.assertTrue(result)
        self.assertEqual(
            events,
            [
                "video",
                "stage:Crop",
                "click:Next",
                "stage:Edit",
                "click:Next",
                "stage:New reel",
                "caption-focus",
                "caption",
            ],
        )

    def test_instagram_publish_clicks_share_only_after_caption(self):
        events = []
        file_input = Mock()
        file_input.send_keys.side_effect = lambda _: events.append("video")
        caption_box = Mock()
        caption_box.click.side_effect = lambda: events.append("caption-focus")
        caption_box.send_keys.side_effect = lambda _: events.append("caption")
        driver = Mock()

        def record_stage(_driver, heading, **_kwargs):
            events.append(f"stage:{heading}")

        def record_click(_driver, labels, **_kwargs):
            events.append(f"click:{labels[0]}")

        with tempfile.TemporaryDirectory() as directory:
            video_path = Path(directory) / "video.mp4"
            video_path.write_bytes(b"video")
            with (
                patch.object(instagram_selenium, "_open_post_composer"),
                patch.object(instagram_selenium, "_find_video_file_input", return_value=file_input),
                patch.object(instagram_selenium, "_find_caption_box", return_value=caption_box),
                patch.object(instagram_selenium, "_wait_for_composer_stage", side_effect=record_stage),
                patch.object(instagram_selenium, "_click_control", side_effect=record_click),
                patch.object(
                    instagram_selenium,
                    "_wait_for_share_completion",
                    side_effect=lambda _driver: events.append("share-confirmed"),
                ),
                patch.object(instagram_selenium.time, "sleep"),
            ):
                result = instagram_selenium.upload_instagram(
                    str(video_path),
                    "test caption",
                    publish=True,
                    driver=driver,
                )

        self.assertTrue(result)
        self.assertEqual(
            events[-4:],
            ["caption-focus", "caption", "click:Share", "share-confirmed"],
        )

    def test_facebook_click_finds_next_inside_unlabelled_container(self):
        control = Mock()
        control.is_displayed.return_value = True
        control.is_enabled.return_value = True
        control.get_attribute.return_value = None
        driver = Mock()

        def find_elements(_by, xpath):
            if "normalize-space(.)='Next'" in xpath:
                return [control]
            return []

        driver.find_elements.side_effect = find_elements
        wait = Mock()
        wait.until.side_effect = lambda condition: condition(driver)

        with patch.object(facebook_selenium, "WebDriverWait", return_value=wait):
            facebook_selenium._click_text(driver, ["Next"])

        control.click.assert_called_once_with()

    def test_facebook_click_escapes_apostrophes_in_control_labels(self):
        control = Mock()
        control.is_displayed.return_value = True
        control.is_enabled.return_value = True
        control.get_attribute.return_value = None
        driver = Mock()

        def find_elements(_by, xpath):
            if "What's on your mind?" in xpath and "normalize-space(.)" in xpath:
                return [control]
            return []

        driver.find_elements.side_effect = find_elements
        wait = Mock()
        wait.until.side_effect = lambda condition: condition(driver)

        with patch.object(facebook_selenium, "WebDriverWait", return_value=wait):
            facebook_selenium._click_text(driver, ["What's on your mind?"])

        control.click.assert_called_once_with()

    def test_managed_facebook_upload_does_not_block_for_terminal_login(self):
        with tempfile.TemporaryDirectory() as directory:
            video_path = Path(directory) / "video.mp4"
            video_path.write_bytes(b"video")
            video_input = Mock()
            video_input.get_attribute.return_value = "image/*,video/*"
            caption_box = Mock()
            driver = Mock()
            driver.find_elements.return_value = [video_input]
            events = []

            def record_click(_driver, labels):
                events.append(f"click:{labels[0]}")

            def record_stage(_driver, stage):
                events.append(f"stage:{stage}")

            caption_box.click.side_effect = lambda: events.append("caption-focus")
            caption_box.send_keys.side_effect = lambda _: events.append("caption")
            with (
                patch.object(facebook_selenium, "wait_for_manual_login") as manual_login,
                patch.object(facebook_selenium, "_open_post_composer") as open_composer,
                patch.object(facebook_selenium, "_find_video_file_input", return_value=video_input),
                patch.object(facebook_selenium, "_click_text", side_effect=record_click),
                patch.object(facebook_selenium, "_wait_for_stage", side_effect=record_stage),
                patch.object(
                    facebook_selenium,
                    "_click_post_when_ready",
                    side_effect=lambda _driver: events.append("click:Post"),
                ),
                patch.object(
                    facebook_selenium,
                    "_wait_for_publish_confirmation",
                    side_effect=lambda _driver: events.append("publish-confirmed"),
                ),
                patch.object(
                    facebook_selenium,
                    "_find_reel_description_box",
                    return_value=caption_box,
                ),
                patch.object(facebook_selenium.time, "sleep"),
            ):
                result = facebook_selenium.upload_facebook(
                    str(video_path),
                    "caption",
                    page_url="https://web.facebook.com/profile.php?id=61595151675865",
                    driver=driver,
                    publish=True,
                )

        self.assertTrue(result)
        manual_login.assert_not_called()
        open_composer.assert_called_once_with(driver)
        video_input.send_keys.assert_called_once_with(str(video_path.resolve()))
        self.assertEqual(
            events,
            [
                "click:Next",
                "stage:Edit reel",
                "click:Next",
                "stage:Reel settings",
                "caption-focus",
                "caption",
                "click:Post",
                "publish-confirmed",
            ],
        )

    def test_facebook_post_button_waits_until_enabled_and_never_clicks_save(self):
        disabled_post = Mock()
        disabled_post.is_displayed.return_value = True
        disabled_post.is_enabled.return_value = True
        disabled_post.get_attribute.side_effect = lambda name: (
            "true" if name == "aria-disabled" else None
        )
        enabled_post = Mock()
        enabled_post.is_displayed.return_value = True
        enabled_post.is_enabled.return_value = True
        enabled_post.get_attribute.return_value = None
        driver = Mock()
        driver.find_elements.side_effect = [[disabled_post], [enabled_post]]
        wait = Mock()
        wait.until.side_effect = lambda condition: (
            condition(driver) or condition(driver)
        )

        with patch.object(facebook_selenium, "WebDriverWait", return_value=wait):
            facebook_selenium._click_post_when_ready(driver, timeout=20)

        disabled_post.click.assert_not_called()
        enabled_post.click.assert_called_once_with()

    def test_facebook_publish_requires_visible_confirmation(self):
        driver = Mock()
        driver.find_element.return_value.text = "Your reel has been shared"
        wait = Mock()
        wait.until.side_effect = lambda condition: condition(driver)

        with patch.object(facebook_selenium, "WebDriverWait", return_value=wait):
            facebook_selenium._wait_for_publish_confirmation(driver)

    def test_facebook_missing_publish_confirmation_is_not_success(self):
        driver = Mock()
        driver.find_element.return_value.text = "Reel settings"
        wait = Mock()
        wait.until.side_effect = TimeoutException()

        with (
            patch.object(facebook_selenium, "WebDriverWait", return_value=wait),
            self.assertRaisesRegex(RuntimeError, "result is unconfirmed"),
        ):
            facebook_selenium._wait_for_publish_confirmation(driver, timeout=1)

    def test_facebook_reel_description_finder_ignores_initial_post_textbox(self):
        initial_box = Mock()
        initial_box.is_displayed.return_value = True
        initial_box.is_enabled.return_value = True
        initial_box.get_attribute.side_effect = lambda name: (
            "What's on your mind?" if name == "aria-label" else ""
        )
        reel_box = Mock()
        reel_box.is_displayed.return_value = True
        reel_box.is_enabled.return_value = True
        reel_box.get_attribute.side_effect = lambda name: (
            "Describe your reel..." if name == "data-placeholder" else ""
        )
        driver = Mock()
        driver.find_elements.return_value = [initial_box, reel_box]
        wait = Mock()
        wait.until.side_effect = lambda condition: condition(driver)

        with patch.object(facebook_selenium, "WebDriverWait", return_value=wait):
            selected = facebook_selenium._find_reel_description_box(driver)

        self.assertIs(selected, reel_box)

    def test_facebook_file_input_selection_skips_image_only_input(self):
        image_input = Mock()
        image_input.get_attribute.return_value = "image/*"
        video_input = Mock()
        video_input.get_attribute.return_value = "video/*,.mp4"
        driver = Mock()
        driver.find_elements.return_value = [image_input, video_input]
        wait = Mock()
        wait.until.side_effect = lambda condition: condition(driver)

        with patch.object(facebook_selenium, "WebDriverWait", return_value=wait):
            selected = facebook_selenium._find_video_file_input(driver)

        self.assertIs(selected, video_input)


if __name__ == "__main__":
    unittest.main()
