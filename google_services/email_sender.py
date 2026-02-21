import asyncio
import logging
import random

from playwright.async_api import Page

from browser.session import new_page

logger = logging.getLogger(__name__)


async def send_daily_summary(items: list[dict], recipient_email: str = "") -> None:
    if not items:
        logger.info("No assignments processed, skipping email")
        return

    page = await new_page()

    try:
        await page.goto("https://mail.google.com/mail/u/0/#inbox", wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(3)

        compose_btn = page.locator('[class*="T-I T-I-KE"], [role="button"]:has-text("Compose")').first
        await compose_btn.click()
        await asyncio.sleep(2)

        to_field = page.locator('[aria-label="To recipients"], [name="to"], input[aria-label*="To"]').first
        await to_field.click()
        await to_field.fill(recipient_email or "me")
        await page.keyboard.press("Enter")
        await asyncio.sleep(0.5)

        subject_field = page.locator('[name="subjectbox"], [aria-label="Subject"]').first
        await subject_field.click()
        await subject_field.fill(f"StudyFlow: {len(items)} Draft(s) Ready for Review")
        await asyncio.sleep(0.3)

        body_editor = page.locator('[role="textbox"][aria-label*="Body"], [contenteditable="true"][aria-label*="Body"]').first
        await body_editor.click()
        await asyncio.sleep(0.3)

        await body_editor.type("StudyFlow Daily Summary", delay=10)
        await page.keyboard.press("Enter")
        await page.keyboard.press("Enter")

        for item in items:
            line = (
                f"• {item['course_name']} — {item['title']} "
                f"(Due: {item['due_date_str']})"
            )
            await body_editor.type(line, delay=8)
            await page.keyboard.press("Enter")

            if item.get("draft_link"):
                await body_editor.type(f"  Draft: {item['draft_link']}", delay=8)
                await page.keyboard.press("Enter")

            if item.get("assignment_link"):
                await body_editor.type(f"  Assignment: {item['assignment_link']}", delay=8)
                await page.keyboard.press("Enter")

            await page.keyboard.press("Enter")
            await asyncio.sleep(0.2)

        await body_editor.type("Review each draft, make edits, then submit.", delay=10)

        await asyncio.sleep(1)
        send_btn = page.locator('[role="button"][aria-label*="Send"], [data-tooltip*="Send"]').first
        await send_btn.click()
        await asyncio.sleep(2)

        logger.info("Summary email sent")

    except Exception:
        logger.exception("Failed to send email via Gmail")
    finally:
        await page.close()
