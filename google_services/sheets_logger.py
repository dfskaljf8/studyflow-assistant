import asyncio
import logging
from datetime import datetime, timezone

from browser.session import new_page, safe_goto
from config.settings import settings

logger = logging.getLogger(__name__)


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
        await safe_goto(page, sheet_url, wait_selector='[class*="cell-input"], #waffle-grid-container')
        await asyncio.sleep(3)

        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        row_data = [now_str, course_name, title, due_date_str, draft_link, status]

        # Find the name box and navigate to the right cell
        name_box = page.locator('[class*="jfk-textinput"], #t-name-box').first
        try:
            await name_box.click()

            # Find next empty row
            last_row = await page.evaluate("""
                () => {
                    const cells = document.querySelectorAll('[class*="cell-input"]');
                    let maxRow = 1;
                    for (const cell of cells) {
                        const parent = cell.closest('tr, [class*="row"]');
                        const row = parent ? parseInt(parent.dataset?.row || '0') : 0;
                        if (row > maxRow) maxRow = row;
                    }
                    return maxRow;
                }
            """)
            next_row = max(last_row + 1, 2)

            await name_box.fill(f"A{next_row}")
            await page.keyboard.press("Enter")
            await asyncio.sleep(0.5)

            for val in row_data:
                await page.keyboard.type(str(val), delay=10)
                await page.keyboard.press("Tab")
                await asyncio.sleep(0.3)

            await asyncio.sleep(1)
            logger.info("Logged to sheet: %s - %s", course_name, title)

        except Exception:
            logger.warning("Could not write to name box, trying direct cell input")

    except Exception:
        logger.exception("Failed to log to sheet")
    finally:
        await page.close()
