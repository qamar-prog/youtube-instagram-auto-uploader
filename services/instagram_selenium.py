from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from selenium.common.exceptions import StaleElementReferenceException, TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from .social_selenium_common import create_driver, wait_for_manual_login

HOME = "https://www.instagram.com/"
LOGGER = logging.getLogger(__name__)
SHARE_COMPLETION_TIMEOUT = 300

_PAGE_INSPECTION_SCRIPT = r"""
const isVisible = (element) => {
  const style = window.getComputedStyle(element);
  const rect = element.getBoundingClientRect();
  return style.visibility !== "hidden" && style.display !== "none" &&
    Number(style.opacity) > 0 && rect.width > 0 && rect.height > 0;
};
const normalize = (value) => (value || "").replace(/\s+/g, " ").trim().toLowerCase();
const safeTestId = (value) => {
  const normalized = normalize(value);
  return /^[a-z_-]{1,48}$/.test(normalized) &&
    /(^|[_-])(create|post|upload)([_-]|$)/.test(normalized)
    ? normalized : "";
};
const candidates = [];
const seen = new Set();
const actionableSelector =
  "button, a, [role='button'], [role='link'], [role='menuitem'], [data-testid]";
const navRoots = [...document.querySelectorAll("nav, [role='navigation']")]
  .filter(isVisible);
const composerRoots = [...document.querySelectorAll(
  "[role='dialog'], dialog[open], [aria-modal='true'], [role='menu']"
)].filter(isVisible);
const exactLabel = (element) =>
  normalize(element.innerText || element.textContent).toLowerCase();
const createMenuRoots = [];
for (const option of [...document.querySelectorAll("a[role='link']")]) {
  if (!isVisible(option) || exactLabel(option) !== "post") continue;
  let ancestor = option.parentElement;
  for (let depth = 0; ancestor && depth < 5; depth++, ancestor = ancestor.parentElement) {
    if (!isVisible(ancestor)) continue;
    const options = [...ancestor.querySelectorAll("a[role='link']")]
      .filter(isVisible)
      .map(exactLabel);
    if (
      options.includes("post") &&
      options.includes("live video") &&
      options.includes("ad")
    ) {
      createMenuRoots.push(ancestor);
      break;
    }
  }
}
const roots = [...navRoots, ...composerRoots, ...createMenuRoots, document.body];
const knownLabels = new Set([
  "create", "post", "new post", "create new post",
  "select from computer", "select from your computer", "select files",
  "next", "share"
]);
for (const root of roots) {
  for (const element of root.querySelectorAll(actionableSelector)) {
    if (!isVisible(element) || seen.has(element)) continue;
    seen.add(element);
    const aria = element.getAttribute("aria-label") || "";
    const title = element.getAttribute("title") || "";
    const testId = element.getAttribute("data-testid") || "";
    const text = element.innerText || element.textContent || "";
    const icon = element.querySelector("svg[aria-label], svg title");
    const iconLabel = icon
      ? (icon.getAttribute("aria-label") || icon.getAttribute("title") || icon.textContent || "")
      : "";
    const stableTestId = safeTestId(testId);
    const labels = [aria, title, stableTestId, iconLabel, text]
      .map(normalize)
      .filter((value) => knownLabels.has(value));
    if (stableTestId) {
      for (const label of ["create", "post", "upload"]) {
        if (new RegExp(`(^|[_-])${label}([_-]|$)`).test(stableTestId)) {
          labels.push(label);
        }
      }
    }
    if (!labels.length) continue;
    const rawHref = element.getAttribute("href");
    let hrefKind = "";
    if (rawHref === "#") hrefKind = "hash";
    else if (rawHref) {
      try {
        const url = new URL(rawHref, location.origin);
        hrefKind = url.origin !== location.origin ? "external" :
          url.hash === "#" ? "hash" :
          (/^\/create\/?$/.test(url.pathname) ? "create-route" : "other-internal");
      } catch {
        hrefKind = "other";
      }
    }
    const observedIconCreateLink = element.tagName.toLowerCase() === "a" &&
      element.getAttribute("role") === "link" &&
      normalize(iconLabel) === "new post";
    if (observedIconCreateLink && !labels.includes("create")) {
      labels.push("create");
    }
    candidates.push({
      scope: composerRoots.some((root) => root.contains(element)) ||
        createMenuRoots.some((root) => root.contains(element))
        ? "composer"
        : navRoots.some((nav) => nav.contains(element))
          ? "navigation" : "page",
      tag: element.tagName.toLowerCase(),
      role: element.getAttribute("role") || "",
      aria_label: knownLabels.has(normalize(aria)) ? normalize(aria) : "",
      title: knownLabels.has(normalize(title)) ? normalize(title) : "",
      icon_label: knownLabels.has(normalize(iconLabel)) ? normalize(iconLabel) : "",
      data_testid: stableTestId,
      labels,
      href_kind: hrefKind
    });
  }
}
const pathname = location.pathname.toLowerCase();
const visibleText = normalize(document.body.innerText || "");
const hasPasswordField = [...document.querySelectorAll("input[type='password']")]
  .some(isVisible);
const hasUserField = [...document.querySelectorAll(
  "input[name='username'], input[autocomplete='username'], input[aria-label*='username' i]"
)].some(isVisible);
const challengePath = /challenge|checkpoint|two_factor|security/.test(pathname);
const challengeText = /\b(security check|confirm your identity|verification code|enter code|checkpoint|captcha)\b/.test(visibleText);
const isChallenge = challengePath || challengeText;
const isLogin = !isChallenge && (
  /\/accounts\/login/.test(pathname) || (hasPasswordField && hasUserField)
);
const hasDialog = [...document.querySelectorAll(
  "[role='dialog'], dialog[open], [aria-modal='true']"
)].some(isVisible) || composerRoots.length > 0 || createMenuRoots.length > 0;
const hasNavigation = navRoots.length > 0;
const safePath = /\/accounts\/login/.test(pathname) ? "/accounts/login/" :
  /challenge|checkpoint|two_factor|security/.test(pathname) ? "/challenge/<redacted>" :
  pathname === "/" ? "/" :
  /^\/[^/]+\/?$/.test(pathname) ? "/<profile-or-private-path>" : "/<other>";
const state = isChallenge ? "security_challenge" :
  isLogin ? "logged_out" :
  hasDialog ? "dialog_open" :
  hasNavigation ? "normal_instagram_page" : "unknown_page";
const createControl = candidates.find((item) =>
  item.labels.includes("create") && item.scope === "navigation" &&
  item.tag === "a" && item.role === "link" &&
  item.icon_label === "new post"
) || (
  pathname === "/" && navRoots.length > 0
    ? candidates.find((item) =>
        item.labels.includes("create") && item.scope === "page" &&
        item.tag === "a" && item.role === "link" &&
        item.icon_label === "new post"
      ) || null
    : null
);
const hasUploadPicker = document.querySelector("input[type='file']") !== null ||
  candidates.some((item) =>
    item.scope === "composer" &&
    item.labels.some((label) => label.startsWith("select from") || label === "select files")
  );
return {
  state,
  safe_url: `${location.origin}${safePath}`,
  page_title_is_instagram: normalize(document.title) === "instagram",
  route_kind: /\/accounts\/login/.test(pathname) ? "login" :
    /challenge|checkpoint|two_factor|security/.test(pathname) ? "challenge" :
    pathname === "/" ? "home" :
    /^\/[^/]+\/?$/.test(pathname) ? "profile_or_private" : "other",
  has_password_field: hasPasswordField,
  has_username_field: hasUserField,
  has_dialog: hasDialog,
  has_navigation: hasNavigation,
  create_control_scope: createControl
    ? createControl.scope === "navigation"
      ? "navigation"
      : "observed_home_icon_fallback"
    : null,
  has_upload_picker: hasUploadPicker,
  composer_post_option_found: candidates.some((item) =>
    item.scope === "composer" && item.labels.includes("post")
  ),
  create_control_found: Boolean(createControl),
  create_control: createControl,
  candidates
};
"""


def _inspect_page(driver) -> dict[str, Any]:
    """Return a minimal DOM summary; never reads cookies or form values."""
    return driver.execute_script(_PAGE_INSPECTION_SCRIPT)


def _page_state_error(snapshot: dict[str, Any]) -> str | None:
    state = snapshot.get("state")
    if state == "logged_out":
        return (
            "Instagram is showing its login page. Sign in manually in the open "
            "Instagram Chrome profile, then retry."
        )
    if state == "security_challenge":
        return (
            "Instagram is showing a security or verification challenge. Complete "
            "it manually in the open Chrome window, then retry."
        )
    return None


def _log_page_state(snapshot: dict[str, Any]) -> None:
    LOGGER.info(
        "Instagram page: url=%s title=%s state=%s login_form=%s "
        "challenge=%s dialog=%s navigation=%s",
        snapshot.get("safe_url", "https://www.instagram.com/<redacted>"),
        "Instagram" if snapshot.get("page_title_is_instagram") else "<redacted>",
        snapshot.get("state", "unknown_page"),
        snapshot.get("state") == "logged_out",
        snapshot.get("state") == "security_challenge",
        snapshot.get("has_dialog", False),
        snapshot.get("has_navigation", False),
    )


def _find_control(driver, label: str):
    snapshot = _inspect_page(driver)
    label = label.strip().lower()
    candidates = snapshot.get("candidates", [])
    if label == "create":
        matching = snapshot.get("create_control") or next(
            (
                item
                for item in candidates
                if label in item.get("labels", [])
                and item.get("tag") == "a"
                and item.get("role") == "link"
                and item.get("icon_label") == "new post"
                and (
                    item.get("scope") == "navigation"
                    or (
                        item.get("scope") == "page"
                        and snapshot.get("route_kind") == "home"
                        and snapshot.get("has_navigation")
                    )
                )
            ),
            None,
        )
        if matching is None:
            return None
        allow_home_fallback = (
            matching.get("scope") == "page"
            and snapshot.get("route_kind") == "home"
            and snapshot.get("has_navigation")
        )
        element = driver.execute_script(
            """
            const isVisible = (element) => {
              const style = getComputedStyle(element);
              const rect = element.getBoundingClientRect();
              return style.visibility !== "hidden" && style.display !== "none" &&
                Number(style.opacity) > 0 && rect.width > 0 && rect.height > 0;
            };
            const isCreateLink = (element) => {
              if (element.getAttribute("role") !== "link" ||
                  element.getAttribute("aria-disabled") === "true" ||
                  element.disabled === true || !isVisible(element)) return false;
              return [...element.querySelectorAll("svg[aria-label], svg title")]
                .some((icon) => (
                  icon.getAttribute("aria-label") ||
                  icon.getAttribute("title") ||
                  icon.textContent ||
                  ""
                ).trim().toLowerCase() === "new post");
            };
            const navigation = [...document.querySelectorAll(
              "nav, [role='navigation']"
            )].find(isVisible);
            if (navigation) {
              const navigationMatch = [...navigation.querySelectorAll("a")]
                .find(isCreateLink);
              if (navigationMatch) return navigationMatch;
            }
            if (!arguments[0] || location.pathname !== "/" || !navigation) {
              return null;
            }
            return [...document.querySelectorAll("a")].find((element) =>
              !element.closest("nav, [role='navigation']") && isCreateLink(element)
            ) || null;
            """,
            bool(allow_home_fallback),
        )
        if (
            element is not None
            and element.is_displayed()
            and element.is_enabled()
        ):
            return element
        return None

    if label in {"next", "share"}:
        matching = next(
            (
                item
                for item in candidates
                if label in item.get("labels", [])
                and item.get("scope") == "composer"
                and (
                    label == "share"
                    or item.get("role") == "button"
                )
            ),
            None,
        )
        if matching is None and label != "share":
            return None
        element = driver.execute_script(
            """
            const visible = (element) => {
              const style = getComputedStyle(element);
              const rect = element.getBoundingClientRect();
              return style.visibility !== "hidden" && style.display !== "none" &&
                Number(style.opacity) > 0 && rect.width > 0 && rect.height > 0;
            };
            const label = (element) =>
              (element.innerText || element.textContent || "")
                .replace(/\\s+/g, " ").trim().toLowerCase();
            const enabled = (element) =>
              element.getAttribute("aria-disabled") !== "true" &&
              element.disabled !== true;
            const composer = [...document.querySelectorAll(
              "[role='dialog'], dialog[open], [aria-modal='true']"
            )].find(visible);
            if (!composer) return null;
            const targetLabel = arguments[0];
            const semanticSelector =
              "button, a, [role='button'], [role='link'], [tabindex='0']";
            const semanticMatch = [...composer.querySelectorAll(semanticSelector)]
              .find((element) =>
                visible(element) && enabled(element) &&
                label(element) === targetLabel
              );
            if (semanticMatch || targetLabel !== "share") {
              return semanticMatch || null;
            }
            return [...composer.querySelectorAll("*")].find((element) =>
              visible(element) && enabled(element) &&
              label(element) === "share" &&
              getComputedStyle(element).cursor === "pointer"
            ) || null;
            """,
            label,
        )
        if (
            element is not None
            and element.is_displayed()
            and element.is_enabled()
        ):
            return element
        return None

    if label == "post":
        matching = next(
            (
                item
                for item in candidates
                if label in item.get("labels", [])
                and item.get("scope") == "composer"
                and item.get("tag") == "a"
                and item.get("role") == "link"
                and item.get("href_kind") == "hash"
            ),
            None,
        )
        if matching is None:
            return None
        element = driver.execute_script(
            """
            const visible = (element) => {
              const style = getComputedStyle(element);
              const rect = element.getBoundingClientRect();
              return style.visibility !== "hidden" && style.display !== "none" &&
                Number(style.opacity) > 0 && rect.width > 0 && rect.height > 0;
            };
            const label = (element) =>
              (element.innerText || element.textContent || "")
                .replace(/\\s+/g, " ").trim().toLowerCase();
            for (const post of document.querySelectorAll("a[role='link']")) {
              if (!visible(post) || label(post) !== "post") continue;
              let ancestor = post.parentElement;
              for (let depth = 0; ancestor && depth < 5; depth++, ancestor = ancestor.parentElement) {
                if (!visible(ancestor)) continue;
                const options = [...ancestor.querySelectorAll("a[role='link']")]
                  .filter(visible)
                  .map(label);
                if (
                  options.includes("post") &&
                  options.includes("live video") &&
                  options.includes("ad")
                ) {
                  return post;
                }
              }
            }
            return null;
            """
        )
        if (
            element is not None
            and element.is_displayed()
            and element.is_enabled()
        ):
            return element
        return None

    matching = next(
        (
            item
            for item in candidates
            if label in item.get("labels", [])
            and (label != "post" or item.get("scope") == "composer")
        ),
        None,
    )
    if matching is None:
        return None
    selector = (
        "button, a, [role='button'], [role='link'], [role='menuitem'], "
        "[data-testid]"
    )
    elements = driver.find_elements(By.CSS_SELECTOR, selector)
    for element in elements:
        try:
            if not element.is_displayed() or not element.is_enabled():
                continue
            if element.tag_name.lower() != matching.get("tag"):
                continue
            attributes_match = all(
                not matching.get(key)
                or element.get_attribute(attribute) == matching[key]
                for key, attribute in (
                    ("role", "role"),
                    ("aria_label", "aria-label"),
                    ("title", "title"),
                    ("data_testid", "data-testid"),
                )
            )
            if not attributes_match:
                continue
            accessible_values = {
                (element.get_attribute("aria-label") or "").strip().lower(),
                (element.get_attribute("title") or "").strip().lower(),
                (element.text or "").strip().lower(),
            }
            icon_label = element.find_elements(By.CSS_SELECTOR, "svg[aria-label], svg title")
            if icon_label:
                accessible_values.add(
                    (
                        icon_label[0].get_attribute("aria-label")
                        or icon_label[0].get_attribute("title")
                        or icon_label[0].text
                        or ""
                    )
                    .strip()
                    .lower()
                )
            test_id = matching.get("data_testid", "")
            if test_id == label or f"_{label}_" in f"_{test_id}_":
                accessible_values.add(label)
            if label not in accessible_values:
                continue
            if matching.get("scope") in {"navigation", "composer"}:
                in_scope = driver.execute_script(
                    """
                    const element = arguments[0];
                    const scope = arguments[1];
                    if (scope === "navigation") {
                      return Boolean(element.closest("nav, [role='navigation']"));
                    }
                    if (scope === "composer") {
                      return Boolean(element.closest(
                          "[role='dialog'], dialog[open], [aria-modal='true'], [role='menu']"
                      ));
                    }
                    if (
                      scope !== "observed_home_icon_fallback" ||
                      location.pathname !== "/" ||
                      !document.querySelector("nav, [role='navigation']")
                    ) return false;
                    try {
                      return element.tagName.toLowerCase() === "a" &&
                        element.getAttribute("role") === "link" &&
                        new URL(element.getAttribute("href"), location.href).hash === "#" &&
                        [...element.querySelectorAll("svg[aria-label], svg title")]
                          .some((icon) =>
                            (icon.getAttribute("aria-label") || icon.getAttribute("title") ||
                              icon.textContent || "").trim().toLowerCase() === "new post"
                          );
                    } catch {
                      return false;
                    }
                    """,
                    element,
                    (
                        matching.get("scope")
                        if matching.get("scope") in {"navigation", "composer"}
                        else "observed_home_icon_fallback"
                    ),
                )
                if not in_scope:
                    continue
            return element
        except StaleElementReferenceException:
            continue
    return None


def _click_control(
    driver,
    labels: list[str],
    timeout: int = 8,
    diagnose_missing: bool = True,
):
    last_snapshot: dict[str, Any] = {}
    for label in labels:
        last_snapshot = _inspect_page(driver)
        state_error = _page_state_error(last_snapshot)
        if state_error:
            raise RuntimeError(state_error)

        def click_when_ready(current_driver):
            element = _find_control(current_driver, label)
            if element is None:
                return False
            try:
                element.click()
            except StaleElementReferenceException:
                return False
            return True

        try:
            WebDriverWait(driver, timeout).until(click_when_ready)
            return
        except TimeoutException:
            continue

    if not diagnose_missing:
        raise RuntimeError(
            f"Instagram control {labels} was not found on page state "
            f"'{last_snapshot.get('state', 'unknown_page')}'."
        )
    raise RuntimeError(
        f"Instagram control {labels} was not found on page state "
        f"'{last_snapshot.get('state', 'unknown_page')}'."
    )


def _open_post_composer(driver):
    snapshot = _inspect_page(driver)
    _log_page_state(snapshot)
    state_error = _page_state_error(snapshot)
    if state_error:
        raise RuntimeError(state_error)

    _click_inspected_control(driver, "create", timeout=8)

    def wait_for_composer_entry(current_driver):
        current = _inspect_page(current_driver)
        if current.get("has_dialog") and (
            current.get("composer_post_option_found")
            or current.get("has_upload_picker")
        ):
            return current
        return False

    try:
        snapshot = WebDriverWait(driver, 12).until(wait_for_composer_entry)
    except TimeoutException as exc:
        snapshot = _inspect_page(driver)
        raise RuntimeError(
            "Instagram's Create control was clicked, but its Post menu did not "
            f"appear. Page state: {snapshot.get('state', 'unknown_page')}."
        ) from exc

    if snapshot.get("has_upload_picker"):
        return

    if not snapshot.get("composer_post_option_found"):
        raise RuntimeError(
            "Instagram's Create menu opened, but no Post option or upload picker "
            "was found."
        )

    _click_inspected_control(driver, "post", timeout=5)

    def wait_for_upload_composer(current_driver):
        current = _inspect_page(current_driver)
        return current if (
            current.get("has_dialog") and current.get("has_upload_picker")
        ) else False

    try:
        WebDriverWait(driver, 20).until(
            wait_for_upload_composer
        )
    except TimeoutException as exc:
        snapshot = _inspect_page(driver)
        raise RuntimeError(
            "Instagram's Post option was selected, but the upload composer did "
            f"not appear. Page state: {snapshot.get('state', 'unknown_page')}."
        ) from exc


def _click_inspected_control(driver, label: str, timeout: int):
    def click_when_ready(current_driver):
        element = _find_control(current_driver, label)
        if element is None:
            return False
        try:
            element.click()
        except StaleElementReferenceException:
            return False
        return True

    try:
        WebDriverWait(driver, timeout).until(click_when_ready)
    except TimeoutException as exc:
        snapshot = _inspect_page(driver)
        raise RuntimeError(
            f"Instagram's observed {label.title()} control could not be clicked "
            "because it was missing, disabled, or changed in the DOM. "
            f"Page state: {snapshot.get('state', 'unknown_page')}."
        ) from exc


def _find_caption_box(driver, timeout=20):
    selectors = [
        (By.CSS_SELECTOR, "textarea[aria-label]"),
        (By.CSS_SELECTOR, "div[contenteditable='true'][role='textbox']"),
        (By.CSS_SELECTOR, "div[contenteditable='true']"),
    ]
    for by, selector in selectors:
        try:
            return WebDriverWait(driver, timeout).until(
                EC.visibility_of_element_located((by, selector))
            )
        except TimeoutException:
            continue
    raise RuntimeError("Instagram caption box was not found; the page layout may have changed.")


def _composer_heading(driver) -> str | None:
    return driver.execute_script(
        """
        const visible = (element) => {
          const style = getComputedStyle(element);
          const rect = element.getBoundingClientRect();
          return style.visibility !== "hidden" && style.display !== "none" &&
            Number(style.opacity) > 0 && rect.width > 0 && rect.height > 0;
        };
        const composer = [...document.querySelectorAll(
          "[role='dialog'], dialog[open], [aria-modal='true']"
        )].find(visible);
        if (!composer) return null;
        const heading = [...composer.querySelectorAll(
          "[role='heading'], h1, h2, h3"
        )].find(visible);
        return heading
          ? (heading.innerText || heading.textContent || "").replace(/\\s+/g, " ").trim()
          : null;
        """
    )


def _wait_for_composer_stage(driver, expected_heading: str, timeout: int = 30) -> None:
    def expected_stage(current_driver):
        heading = _composer_heading(current_driver)
        return heading if heading == expected_heading else False

    try:
        WebDriverWait(driver, timeout).until(expected_stage)
    except TimeoutException as exc:
        snapshot = _inspect_page(driver)
        state_error = _page_state_error(snapshot)
        if state_error:
            raise RuntimeError(state_error) from exc
        raise RuntimeError(
            f"Instagram did not reach the '{expected_heading}' upload stage; "
            f"current composer heading is {_composer_heading(driver)!r}."
        ) from exc


def _share_completion_state(driver) -> dict[str, Any]:
    return driver.execute_script(
        r"""
        const visible = (element) => {
          const style = getComputedStyle(element);
          const rect = element.getBoundingClientRect();
          return style.visibility !== "hidden" && style.display !== "none" &&
            Number(style.opacity) > 0 && rect.width > 0 && rect.height > 0;
        };
        const normalize = (value) =>
          (value || "").replace(/\\s+/g, " ").trim().toLowerCase();
        const bodyText = normalize(document.body.innerText || "");
        const heading = [...document.querySelectorAll(
          "[role='heading'], h1, h2, h3"
        )].find(visible);
        const headingText = heading
          ? normalize(heading.innerText || heading.textContent || "")
          : "";
        const success = /your (reel|post|video) has been shared|your (reel|post|video) was shared|(reel|post) shared successfully/.test(bodyText);
        const failurePattern = /(?:couldn't|could not)\s+(?:be\s+)?(?:shared|uploaded)|failed to\s+(?:share|upload)|something went wrong/;
        const failure = failurePattern.test(bodyText);
        const failureDetails = [...document.querySelectorAll(
          "h1, h2, h3, [role='heading'], p, button"
        )]
          .filter(visible)
          .map((element) => normalize(element.innerText || element.textContent || ""))
          .filter((text) => failurePattern.test(text))
          .filter((text, index, messages) => messages.indexOf(text) === index)
          .slice(0, 3);
        return {
          sharing: headingText === "sharing" || /(^| )sharing( |$)/.test(bodyText),
          success,
          failure,
          failure_message: failureDetails.join(" ").slice(0, 300),
          composer_open: ["crop", "edit", "new reel", "create new post"]
            .includes(headingText),
          navigation: [...document.querySelectorAll(
            "nav, [role='navigation']"
          )].some(visible)
        };
        """
    )


def _wait_for_share_completion(driver, timeout: int = SHARE_COMPLETION_TIMEOUT) -> None:
    saw_sharing = False
    completed_observations = 0

    def completed(current_driver):
        nonlocal saw_sharing, completed_observations
        state = _share_completion_state(current_driver)
        saw_sharing = saw_sharing or state["sharing"]
        if state["success"]:
            return True
        if state["failure"]:
            failure_message = state.get("failure_message") or (
                "Instagram said the post could not be shared."
            )
            raise RuntimeError(
                f"Instagram reported: {failure_message} Check the Instagram "
                "window for further details before retrying to avoid a duplicate post."
            )
        if (
            saw_sharing
            and not state["sharing"]
            and not state["composer_open"]
            and state["navigation"]
        ):
            completed_observations += 1
            return completed_observations >= 3
        completed_observations = 0
        return False

    try:
        WebDriverWait(driver, timeout).until(completed)
    except TimeoutException as exc:
        raise RuntimeError(
            "Instagram did not confirm that sharing finished within "
            f"{timeout} seconds. The result is unconfirmed; check Instagram "
            "before retrying to avoid a duplicate post."
        ) from exc


def _find_video_file_input(driver, timeout=20):
    selector = "input[type='file']"
    try:
        return WebDriverWait(driver, 5).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, selector))
        )
    except TimeoutException:
        try:
            _click_control(
                driver,
                [
                    "Select from computer",
                    "Select from your computer",
                    "Select files",
                ],
                timeout=4,
                diagnose_missing=False,
            )
        except RuntimeError:
            pass

    try:
        return WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, selector))
        )
    except TimeoutException as exc:
        snapshot = _inspect_page(driver)
        raise RuntimeError(
            "Instagram's video upload input did not appear. "
            f"Page state: {snapshot.get('state', 'unknown_page')}."
        ) from exc


def upload_instagram(
    video_path: str,
    caption: str,
    publish: bool = False,
    driver=None,
):
    """
    Best-effort Instagram browser upload using the persistent signed-in profile.
    """
    path = Path(video_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Video not found: {path}")

    owns_driver = driver is None
    driver = driver or create_driver("instagram")
    try:
        if owns_driver:
            wait_for_manual_login(driver, "Instagram", HOME)
        driver.get(HOME)
        time.sleep(3)

        _open_post_composer(driver)
        file_input = _find_video_file_input(driver)
        file_input.send_keys(str(path))

        _wait_for_composer_stage(driver, "Crop", timeout=60)
        _click_control(driver, ["Next"], timeout=20)
        _wait_for_composer_stage(driver, "Edit")
        _click_control(driver, ["Next"], timeout=20)
        _wait_for_composer_stage(driver, "New reel")

        caption_box = _find_caption_box(driver)
        caption_box.click()
        caption_box.send_keys(caption)

        LOGGER.info("Instagram video and caption are prepared.")
        if publish:
            _click_control(driver, ["Share"])
            LOGGER.info(
                "Instagram Share was clicked; waiting up to %s seconds for confirmation.",
                SHARE_COMPLETION_TIMEOUT,
            )
            _wait_for_share_completion(driver)
            LOGGER.info("Instagram confirmed the video was shared.")
        else:
            LOGGER.info("Preview mode: Share was not clicked.")
        return True
    finally:
        if publish and owns_driver:
            driver.quit()
