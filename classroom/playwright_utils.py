import logging
import sys

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator, Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_chain, wait_fixed

logger = logging.getLogger(__name__)

# macOS uses Meta (Cmd), everything else (Chromebook/Linux/Windows) uses Control
MOD_KEY = "Meta" if sys.platform == "darwin" else "Control"

DEFAULT_TIMEOUT_MS = 90_000
SUBMISSION_SELECTOR = 'textarea, div[contenteditable="true"], [role="textbox"], input[type="text"]'


def _retry_policy():
    return retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_chain(wait_fixed(2), wait_fixed(5), wait_fixed(10)),
        retry=retry_if_exception_type((PlaywrightTimeoutError, PlaywrightError, RuntimeError)),
    )


def apply_default_timeouts(page: Page, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> None:
    page.set_default_timeout(timeout_ms)
    page.set_default_navigation_timeout(timeout_ms)


@_retry_policy()
async def goto_with_retry(page: Page, url: str, wait_until: str = "domcontentloaded", timeout_ms: int = DEFAULT_TIMEOUT_MS) -> None:
    await page.goto(url, wait_until=wait_until, timeout=timeout_ms)


@_retry_policy()
async def click_with_retry(locator: Locator, timeout_ms: int = 20_000) -> None:
    await locator.click(timeout=timeout_ms)


@_retry_policy()
async def fill_with_retry(locator: Locator, value: str, timeout_ms: int = 20_000) -> None:
    await locator.fill(value, timeout=timeout_ms)


@_retry_policy()
async def wait_for_submission_surface(
    page: Page,
    selector: str = SUBMISSION_SELECTOR,
    networkidle_timeout_ms: int = 45_000,
    selector_timeout_ms: int = 60_000,
) -> None:
    await page.wait_for_load_state("networkidle", timeout=networkidle_timeout_ms)
    await page.wait_for_selector(selector, state="visible", timeout=selector_timeout_ms)
