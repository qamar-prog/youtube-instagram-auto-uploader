from pathlib import Path
from selenium import webdriver
from selenium.common.exceptions import SessionNotCreatedException
from selenium.webdriver.support.ui import WebDriverWait

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROFILE_ROOT = PROJECT_ROOT / "browser_profiles"
PROFILE_ROOT.mkdir(parents=True, exist_ok=True)


def create_driver(platform: str) -> webdriver.Chrome:
    """Start Chrome with a separate persistent profile for each platform."""
    profile = PROFILE_ROOT / platform
    profile.mkdir(parents=True, exist_ok=True)

    options = webdriver.ChromeOptions()
    options.add_argument(f"--user-data-dir={profile.resolve()}")
    options.add_argument("--start-maximized")
    options.add_experimental_option(
        "prefs",
        {"profile.default_content_setting_values.notifications": 2},
    )
    try:
        return webdriver.Chrome(options=options)
    except SessionNotCreatedException as exc:
        raise RuntimeError(
            f"Chrome could not start with the saved {platform} browser profile. "
            "Close any Chrome window opened by this app, make sure another "
            "backend process is not using the profile, then restart the backend "
            "and try again. If it still fails, update Chrome and Selenium."
        ) from exc


def wait_for_manual_login(driver, platform: str, home_url: str, timeout: int = 300):
    """
    Open the platform and allow the owner to log in and complete any security
    checks manually. This deliberately does not solve or bypass CAPTCHA/2FA.
    The persistent profile can preserve the session between runs.
    """
    driver.get(home_url)
    print(f"\n{platform}: Chrome is open.")
    print("If needed, sign in yourself and complete security checks.")
    input("When you are logged in and the platform page is ready, press Enter here...")
    WebDriverWait(driver, timeout).until(
        lambda d: d.execute_script("return document.readyState") in ("interactive", "complete")
    )
