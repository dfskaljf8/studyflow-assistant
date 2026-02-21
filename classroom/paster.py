import asyncio
import logging
import random

from playwright.async_api import Page

from browser.session import new_page
from classroom.scanner import Assignment

logger = logging.getLogger(__name__)


async def paste_draft(assignment: Assignment, draft_text: str) -> bool:
    if not assignment.assignment_url:
        logger.warning("No URL for: %s", assignment.title)
        return False

    page = await new_page()

    try:
        await page.goto(assignment.assignment_url, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(random.uniform(2, 4))

        add_btn_selectors = [
            'button:has-text("Add or create")',
            'button:has-text("Add work")',
            '[aria-label="Add or create"]',
            '[data-guidedhelpid="assignmentAddWorkButton"]',
        ]

        for selector in add_btn_selectors:
            try:
                btn = page.locator(selector).first
                if await btn.is_visible(timeout=2000):
                    await btn.click()
                    await asyncio.sleep(1.5)
                    break
            except Exception:
                continue

        editor_selectors = [
            '[contenteditable="true"]',
            'div[role="textbox"]',
            ".editable",
            "textarea",
        ]

        editor = None
        for selector in editor_selectors:
            try:
                loc = page.locator(selector).first
                if await loc.is_visible(timeout=2000):
                    editor = loc
                    break
            except Exception:
                continue

        if not editor:
            logger.warning("No text editor found for: %s", assignment.title)
            return False

        await editor.click()
        await asyncio.sleep(0.5)

        lines = draft_text.split("\n")
        for i, line in enumerate(lines):
            await editor.type(line, delay=random.uniform(15, 40))
            if i < len(lines) - 1:
                await page.keyboard.press("Enter")
            await asyncio.sleep(random.uniform(0.1, 0.5))

        logger.info("Draft pasted for: %s", assignment.title)
        await asyncio.sleep(2)
        return True

    except Exception:
        logger.exception("Paste failed for: %s", assignment.title)
        return False
    finally:
        await page.close()
