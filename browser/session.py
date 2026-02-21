import asyncio
import logging

from playwright.async_api import async_playwright, BrowserContext, Page

from browser.stealth import apply_stealth
from config.settings import settings

logger = logging.getLogger(__name__)

_pw = None
_context: BrowserContext | None = None
_main_page: Page | None = None


async def get_browser_context() -> BrowserContext:
    global _pw, _context

    if _context:
        return _context

    settings.browser_data_dir.mkdir(parents=True, exist_ok=True)

    # Remove stale lock file from unclean shutdown
    lock_file = settings.browser_data_dir / "SingletonLock"
    if lock_file.exists():
        lock_file.unlink()
        logger.info("Removed stale SingletonLock")

    _pw = await async_playwright().start()
    _context = await _pw.chromium.launch_persistent_context(
        user_data_dir=str(settings.browser_data_dir),
        headless=False,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
        ],
        viewport={"width": 1366, "height": 900},
        locale="en-US",
    )

    logger.info("Browser launched with persistent session")
    return _context


async def get_page() -> Page:
    """Return a single reused page to keep session/cookies consistent."""
    global _main_page
    if _main_page and not _main_page.is_closed():
        return _main_page

    ctx = await get_browser_context()
    if ctx.pages:
        _main_page = ctx.pages[0]
    else:
        _main_page = await ctx.new_page()
    await apply_stealth(_main_page)
    return _main_page


async def new_page() -> Page:
    """Open an extra tab (for parallel work). Prefer get_page() for most ops."""
    ctx = await get_browser_context()
    page = await ctx.new_page()
    await apply_stealth(page)
    return page


async def safe_goto(page: Page, url: str, wait_selector: str | None = None, timeout: int = 60000) -> None:
    """Navigate and optionally wait for a selector to appear."""
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
    except Exception:
        logger.warning("Slow page load for %s, continuing anyway", url)

    # Always give the SPA time to hydrate
    await asyncio.sleep(3)

    if wait_selector:
        try:
            await page.wait_for_selector(wait_selector, timeout=15000)
        except Exception:
            logger.debug("Selector %s not found on %s, continuing", wait_selector, url)


async def check_logged_in(page: Page) -> bool:
    print("\nOpening Google sign-in...")

    # Go directly to Google Accounts sign-in targeting Classroom
    sign_in_url = (
        "https://accounts.google.com/ServiceLogin"
        "?continue=https://classroom.google.com"
        "&passive=true"
    )
    try:
        await page.goto(sign_in_url, wait_until="domcontentloaded", timeout=60000)
    except Exception:
        pass

    await asyncio.sleep(3)

    # Check if already signed in (redirected straight to Classroom)
    current = page.url
    if "classroom.google.com" in current and "accounts.google.com" not in current:
        print("Already signed in!")
        logger.info("Already signed in to Classroom")
        return True

    print("\n" + "=" * 60)
    print("  SIGN IN TO YOUR SCHOOL ACCOUNT")
    print("")
    print("  A browser window should be open showing Google sign-in.")
    print("  1) Sign into your SCHOOL Google account")
    print("  2) Wait until you see your Google Classroom homepage")
    print("  3) Then come back here and press Enter")
    print("=" * 60)

    await asyncio.get_event_loop().run_in_executor(
        None, lambda: input("\nPress Enter after you see Classroom... ")
    )

    await asyncio.sleep(2)
    final_url = page.url
    if "classroom.google.com" in final_url or "classroom.google.com" in (await page.title()).lower():
        logger.info("Sign-in complete")
        print("Signed in successfully!")
        return True
    else:
        # One more try: navigate to classroom now that cookies should be set
        try:
            await page.goto("https://classroom.google.com", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3)
            if "classroom.google.com" in page.url:
                logger.info("Sign-in complete (after redirect)")
                print("Signed in successfully!")
                return True
        except Exception:
            pass

        logger.warning("May not be signed in — current URL: %s", final_url)
        print(f"Warning: current page: {final_url}")
        print("Try running 'python main.py login' again.")
        return False


async def close_browser():
    global _pw, _context, _main_page
    _main_page = None
    if _context:
        await _context.close()
        _context = None
    if _pw:
        await _pw.stop()
        _pw = None
    logger.info("Browser closed")
