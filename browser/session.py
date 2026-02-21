import asyncio
import logging

from playwright.async_api import async_playwright, BrowserContext, Page

from browser.stealth import apply_stealth
from config.settings import settings

logger = logging.getLogger(__name__)

_pw = None
_context: BrowserContext | None = None


async def get_browser_context() -> BrowserContext:
    global _pw, _context

    if _context:
        return _context

    settings.browser_data_dir.mkdir(parents=True, exist_ok=True)

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


async def new_page() -> Page:
    ctx = await get_browser_context()
    page = await ctx.new_page()
    await apply_stealth(page)
    return page


async def check_logged_in(page: Page) -> bool:
    print("\nOpening Google Classroom...")

    try:
        await page.goto("https://classroom.google.com", wait_until="domcontentloaded", timeout=60000)
    except Exception:
        pass

    await asyncio.sleep(3)

    print("\n" + "=" * 60)
    print("  SIGN IN TO YOUR SCHOOL ACCOUNT")
    print("")
    print("  A browser window should be open.")
    print("  1) Sign into your SCHOOL Google account")
    print("  2) Wait until you see your Google Classroom homepage")
    print("  3) Then come back here and press Enter")
    print("=" * 60)

    await asyncio.get_event_loop().run_in_executor(
        None, lambda: input("\nPress Enter after you see Classroom... ")
    )

    await asyncio.sleep(2)
    final_url = page.url
    if "classroom.google.com" in final_url and "accounts.google.com" not in final_url:
        logger.info("Sign-in complete")
        print("Signed in successfully!")
        return True
    else:
        logger.warning("May not be signed in — current URL: %s", final_url)
        print(f"Warning: doesn't look like Classroom loaded. Current page: {final_url}")
        print("Try running 'python main.py login' again.")
        return False


async def close_browser():
    global _pw, _context
    if _context:
        await _context.close()
        _context = None
    if _pw:
        await _pw.stop()
        _pw = None
    logger.info("Browser closed")
