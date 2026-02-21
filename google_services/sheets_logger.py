import asyncio
import logging
from datetime import datetime, timezone

from playwright.async_api import Page

from browser.session import new_page
from config.settings import settings

logger = logging.getLogger(__name__)


async def _ensure_headers(page: Page) -> None:
    first_cell = page.locator('[class*="cell-input"]').first
    try:
        cell_text = await page.evaluate("""
            () => {
                const cell = document.querySelector('.cell-input');
                return cell ? cell.textContent.trim() : '';
            }
        """)
        if cell_text == "Date":
            return
    except Exception:
        pass

    headers = ["Date", "Class", "Assignment", "Due Date", "Draft Doc Link", "Status"]
    for i, header in enumerate(headers):
        cell_name = f"{'ABCDEF'[i]}1"
        name_box = page.locator('[class*="jfk-textinput"]').first
        try:
            await name_box.click()
            await name_box.fill(cell_name)
            await page.keyboard.press("Enter")
            await asyncio.sleep(0.3)
            await page.keyboard.type(header)
            await page.keyboard.press("Tab")
            await asyncio.sleep(0.2)
        except Exception:
            pass


async def log_assignment(
    course_name: str,
    title: str,
    due_date_str: str,
    draft_link: str,
    status: str = "Draft Pasted - Ready for Review",
) -> None:
    sheet_url = settings.studyflow_sheet_url
    if not sheet_url:
        logger.warning("No STUDYFLOW_SHEET_URL configured, skipping log")
        return

    page = await new_page()

    try:
        await page.goto(sheet_url, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(2)

        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        row_data = [now_str, course_name, title, due_date_str, draft_link, status]

        last_row = await page.evaluate("""
            () => {
                const cells = document.querySelectorAll('.cell-input');
                let maxRow = 1;
                for (const cell of cells) {
                    const row = parseInt(cell.closest('[class*="row"]')?.dataset?.row || '0');
                    if (row > maxRow) maxRow = row;
                }
                return maxRow;
            }
        """)
        next_row = last_row + 1 if last_row > 0 else 2

        name_box = page.locator('[class*="jfk-textinput"]').first
        await name_box.click()
        await name_box.fill(f"A{next_row}")
        await page.keyboard.press("Enter")
        await asyncio.sleep(0.3)

        for val in row_data:
            await page.keyboard.type(str(val), delay=10)
            await page.keyboard.press("Tab")
            await asyncio.sleep(0.2)

        await asyncio.sleep(1)
        logger.info("Logged to sheet: %s - %s", course_name, title)

    except Exception:
        logger.exception("Failed to log to sheet")
    finally:
        await page.close()
