import asyncio
import logging
import random
import time

from playwright.async_api import Page

from browser.session import get_page, safe_goto
from classroom.scanner import Assignment

logger = logging.getLogger(__name__)

PASTE_RUNTIME_BUDGET_SECONDS = 90
INSERT_CHUNK_SIZE = 800


def _normalize_text(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def _chunk_text(text: str, chunk_size: int) -> list[str]:
    if not text:
        return [""]
    return [text[i:i + chunk_size] for i in range(0, len(text), chunk_size)]


async def _clear_editor(page: Page, editor) -> None:
    await editor.click()
    await asyncio.sleep(0.1)
    try:
        await page.keyboard.press("Meta+A")
    except Exception:
        await page.keyboard.press("Control+A")
    await asyncio.sleep(0.05)
    await page.keyboard.press("Backspace")
    await asyncio.sleep(0.1)


async def _editor_non_whitespace_len(editor) -> int:
    try:
        return await editor.evaluate(
            """(el) => {
                const tag = (el.tagName || '').toLowerCase();
                let text = '';
                if (tag === 'textarea' || tag === 'input') {
                    text = el.value || '';
                } else if (el.isContentEditable) {
                    text = el.innerText || el.textContent || '';
                } else {
                    text = el.textContent || '';
                }
                return text.replace(/\\s+/g, '').length;
            }"""
        )
    except Exception:
        return 0


def _looks_pasted(actual_non_ws: int, expected_non_ws: int) -> bool:
    if expected_non_ws == 0:
        return actual_non_ws == 0
    threshold = expected_non_ws if expected_non_ws < 20 else int(expected_non_ws * 0.75)
    return actual_non_ws >= threshold


async def _try_direct_set(editor, text: str) -> bool:
    try:
        await editor.evaluate(
            """(el, value) => {
                const tag = (el.tagName || '').toLowerCase();
                if (tag === 'textarea' || tag === 'input') {
                    el.value = value;
                } else if (el.isContentEditable) {
                    el.focus();
                    if (document.queryCommandSupported && document.queryCommandSupported('insertText')) {
                        document.execCommand('selectAll', false);
                        document.execCommand('insertText', false, value);
                    } else {
                        el.textContent = value;
                    }
                } else {
                    el.textContent = value;
                }
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
            }""",
            text,
        )
        return True
    except Exception:
        return False


async def _try_keyboard_insert(page: Page, editor, text: str) -> bool:
    try:
        await _clear_editor(page, editor)
        for chunk in _chunk_text(text, INSERT_CHUNK_SIZE):
            await page.keyboard.insert_text(chunk)
            await asyncio.sleep(random.uniform(0.01, 0.03))
        return True
    except Exception:
        return False


async def _fallback_type(editor, page: Page, text: str, started_at: float) -> bool:
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if time.monotonic() - started_at > PASTE_RUNTIME_BUDGET_SECONDS:
            logger.warning("Fallback typing exceeded %ds budget", PASTE_RUNTIME_BUDGET_SECONDS)
            return False
        await editor.type(line, delay=random.uniform(2, 6))
        if i < len(lines) - 1:
            await page.keyboard.press("Enter")
        await asyncio.sleep(random.uniform(0.01, 0.04))
    return True


async def paste_draft(assignment: Assignment, draft_text: str) -> bool:
    if not assignment.assignment_url:
        logger.warning("No URL for: %s", assignment.title)
        return False

    page = await get_page()

    try:
        await safe_goto(page, assignment.assignment_url, wait_selector='[role="main"]')
        await asyncio.sleep(random.uniform(3, 5))

        # Look for "Add or create" / "Add work" button
        add_btn_selectors = [
            'button:has-text("Add or create")',
            'button:has-text("Add work")',
            '[aria-label="Add or create"]',
            '[data-guidedhelpid="assignmentAddWorkButton"]',
        ]

        for selector in add_btn_selectors:
            try:
                btn = page.locator(selector).first
                if await btn.is_visible(timeout=3000):
                    await btn.click()
                    await asyncio.sleep(2)
                    break
            except Exception:
                continue

        # Look for text editor
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
                if await loc.is_visible(timeout=3000):
                    editor = loc
                    break
            except Exception:
                continue

        if not editor:
            logger.warning("No text editor found for: %s", assignment.title)
            return False

        draft_text = _normalize_text(draft_text)
        expected_non_ws = len("".join(draft_text.split()))
        started_at = time.monotonic()

        await editor.click()
        await asyncio.sleep(0.2)

        pasted = False

        if await _try_direct_set(editor, draft_text):
            actual_non_ws = await _editor_non_whitespace_len(editor)
            if _looks_pasted(actual_non_ws, expected_non_ws):
                pasted = True
                logger.info("Draft pasted via direct set for: %s", assignment.title)

        if not pasted and (time.monotonic() - started_at) < PASTE_RUNTIME_BUDGET_SECONDS:
            if await _try_keyboard_insert(page, editor, draft_text):
                actual_non_ws = await _editor_non_whitespace_len(editor)
                if _looks_pasted(actual_non_ws, expected_non_ws):
                    pasted = True
                    logger.info("Draft pasted via keyboard insert for: %s", assignment.title)

        if not pasted and (time.monotonic() - started_at) < PASTE_RUNTIME_BUDGET_SECONDS:
            await _clear_editor(page, editor)
            if await _fallback_type(editor, page, draft_text, started_at):
                actual_non_ws = await _editor_non_whitespace_len(editor)
                pasted = _looks_pasted(actual_non_ws, expected_non_ws)
                if pasted:
                    logger.info("Draft pasted via typed fallback for: %s", assignment.title)

        if not pasted:
            elapsed = time.monotonic() - started_at
            logger.warning(
                "Paste may be incomplete for %s (elapsed %.1fs, expected_non_ws=%d)",
                assignment.title,
                elapsed,
                expected_non_ws,
            )
            return False

        logger.info("Draft pasted for: %s", assignment.title)
        await asyncio.sleep(2)
        return True

    except Exception:
        logger.exception("Paste failed for: %s", assignment.title)
        return False
