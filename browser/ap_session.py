import asyncio
import logging
import random

from playwright.async_api import async_playwright, BrowserContext, Page

from browser.stealth import apply_stealth
from config.settings import settings

logger = logging.getLogger(__name__)

_ap_pw = None
_ap_context: BrowserContext | None = None
_ap_page: Page | None = None

AP_SESSION_FILE = settings.project_root / "ap_session.json"
AP_DASHBOARD_URL = "https://myap.collegeboard.org"
AP_LOGIN_URL = "https://myap.collegeboard.org/login"
AP_VIEWPORT = {"width": 1366, "height": 768}
DEFAULT_TIMEOUT_MS = 90000


def ap_session_exists() -> bool:
    return AP_SESSION_FILE.is_file() and AP_SESSION_FILE.stat().st_size > 50


async def _random_mouse_moves(page: Page, count: int = 4) -> None:
    for _ in range(count):
        x = random.randint(100, 1200)
        y = random.randint(100, 650)
        await page.mouse.move(x, y)
        await asyncio.sleep(random.uniform(0.15, 0.45))


async def _launch_ap_context(*, headless: bool, storage_state: str | None = None) -> BrowserContext:
    global _ap_pw
    _ap_pw = await async_playwright().start()

    launch_args = [
        "--disable-blink-features=AutomationControlled",
        "--no-sandbox",
    ]
    ctx_kwargs: dict = {
        "viewport": AP_VIEWPORT,
        "locale": "en-US",
        "user_agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    }
    if storage_state:
        ctx_kwargs["storage_state"] = storage_state

    browser = await _ap_pw.chromium.launch(
        headless=headless,
        args=launch_args,
    )
    context = await browser.new_context(**ctx_kwargs)
    context.set_default_timeout(DEFAULT_TIMEOUT_MS)
    context.set_default_navigation_timeout(DEFAULT_TIMEOUT_MS)
    return context


async def _is_on_ap_dashboard(page: Page) -> bool:
    url = (page.url or "").lower()
    if "myap.collegeboard.org" in url and "/login" not in url:
        return True
    try:
        text = await page.evaluate("() => (document.body?.innerText || '').slice(0,600)")
        if "my ap" in text.lower() or "ap classroom" in text.lower():
            return True
    except Exception:
        pass
    return False


async def ap_first_run_login() -> bool:
    """First-run flow: open headful browser, user signs in manually, session saved."""
    global _ap_context, _ap_page

    print("\n" + "=" * 60)
    print("  AP CLASSROOM - FIRST TIME SETUP")
    print("=" * 60)
    print("  A visible browser window will open to College Board login.")
    print("  Please complete these steps in the browser:")
    print("    1) Click 'Student'")
    print("    2) Enter your College Board email and password")
    print("    3) Complete any 2FA / verification if prompted")
    print("    4) Wait until you see your My AP dashboard")
    print("  The script will detect the dashboard automatically.")
    print("=" * 60 + "\n")

    _ap_context = await _launch_ap_context(headless=False)
    _ap_page = await _ap_context.new_page()
    await apply_stealth(_ap_page)
    await _random_mouse_moves(_ap_page)

    try:
        await _ap_page.goto(AP_LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
    except Exception:
        pass

    try:
        await _ap_page.wait_for_load_state("networkidle", timeout=60000)
    except Exception:
        pass

    print("Waiting for you to sign in (up to 10 minutes)...")
    import time
    start = time.time()
    max_wait = 600
    last_msg = -1
    while time.time() - start < max_wait:
        if await _is_on_ap_dashboard(_ap_page):
            break
        elapsed = int(time.time() - start)
        bucket = elapsed // 30
        if bucket != last_msg:
            last_msg = bucket
            print(f"  Still waiting... {elapsed}s elapsed")
        await asyncio.sleep(2)

    await asyncio.sleep(2)

    if not await _is_on_ap_dashboard(_ap_page):
        try:
            await _ap_page.goto(AP_DASHBOARD_URL, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3)
        except Exception:
            pass

    if await _is_on_ap_dashboard(_ap_page):
        await _ap_context.storage_state(path=str(AP_SESSION_FILE))
        logger.info("AP session saved to %s", AP_SESSION_FILE)
        print("AP session saved. Future runs will use it automatically.")
        return True

    logger.warning("AP dashboard not detected after manual login")
    print("Warning: Could not detect AP dashboard. Try running 'python main.py ap-login' again.")
    return False


async def get_ap_context() -> BrowserContext:
    """Return an AP browser context, loading saved session if available."""
    global _ap_context
    if _ap_context:
        return _ap_context

    if ap_session_exists():
        logger.info("Loading saved AP session from %s", AP_SESSION_FILE)
        _ap_context = await _launch_ap_context(
            headless=True,
            storage_state=str(AP_SESSION_FILE),
        )
    else:
        ok = await ap_first_run_login()
        if not ok:
            raise RuntimeError("AP login required. Run: python main.py ap-login")
    return _ap_context


async def get_ap_page() -> Page:
    """Return a reused AP page with stealth applied."""
    global _ap_page
    if _ap_page and not _ap_page.is_closed():
        return _ap_page

    ctx = await get_ap_context()
    if ctx.pages:
        _ap_page = ctx.pages[0]
    else:
        _ap_page = await ctx.new_page()
    _ap_page.set_default_timeout(DEFAULT_TIMEOUT_MS)
    _ap_page.set_default_navigation_timeout(DEFAULT_TIMEOUT_MS)
    await apply_stealth(_ap_page)
    await _random_mouse_moves(_ap_page)
    return _ap_page


async def check_ap_session_valid() -> bool:
    """Verify saved AP session still works by loading the dashboard."""
    if not ap_session_exists():
        return False
    try:
        page = await get_ap_page()
        await page.goto(AP_DASHBOARD_URL, wait_until="domcontentloaded", timeout=45000)
        try:
            await page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:
            pass
        return await _is_on_ap_dashboard(page)
    except Exception:
        return False


async def refresh_ap_session() -> bool:
    """Save current AP context state back to session file."""
    if _ap_context:
        try:
            await _ap_context.storage_state(path=str(AP_SESSION_FILE))
            logger.info("AP session refreshed")
            return True
        except Exception:
            pass
    return False


async def close_ap_browser() -> None:
    global _ap_pw, _ap_context, _ap_page
    if _ap_context:
        try:
            await _ap_context.storage_state(path=str(AP_SESSION_FILE))
        except Exception:
            pass
    _ap_page = None
    if _ap_context:
        try:
            await _ap_context.close()
        except Exception:
            pass
        _ap_context = None
    if _ap_pw:
        try:
            await _ap_pw.stop()
        except Exception:
            pass
        _ap_pw = None
    logger.info("AP browser closed")
