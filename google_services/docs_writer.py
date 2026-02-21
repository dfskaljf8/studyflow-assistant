import asyncio
import logging
import random

from browser.session import new_page, safe_goto

logger = logging.getLogger(__name__)


async def create_draft_doc(title: str, body_text: str) -> str:
    # Use a new tab for Docs so we don't lose Classroom session in main page
    page = await new_page()

    try:
        await safe_goto(page, "https://docs.google.com/document/create",
                        wait_selector='[contenteditable="true"]')
        await asyncio.sleep(3)

        # Try to rename the doc
        title_input = page.locator('[class*="docs-title-input"], input[aria-label="Rename"]').first
        try:
            if await title_input.is_visible(timeout=5000):
                await title_input.click()
                await asyncio.sleep(0.5)
                await page.keyboard.press("Meta+A")
                await title_input.fill(f"{title} - Draft")
                await page.keyboard.press("Enter")
                await asyncio.sleep(1)
        except Exception:
            logger.warning("Could not set doc title, continuing with body")

        editor = page.locator('[contenteditable="true"]').first
        await editor.click()
        await asyncio.sleep(0.5)

        chunks = body_text.split("\n")
        for i, chunk in enumerate(chunks):
            if chunk.strip():
                await editor.type(chunk, delay=random.uniform(5, 15))
            if i < len(chunks) - 1:
                await page.keyboard.press("Enter")
            await asyncio.sleep(random.uniform(0.05, 0.2))

        await asyncio.sleep(2)

        doc_url = page.url
        logger.info("Created draft doc: %s -> %s", title, doc_url)
        return doc_url

    except Exception:
        logger.exception("Failed to create doc for: %s", title)
        return ""
    finally:
        await page.close()
