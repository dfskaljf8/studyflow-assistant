import asyncio
import logging

from playwright.async_api import Page

from browser.session import new_page, safe_goto

logger = logging.getLogger(__name__)

GMAIL_GOTO_TIMEOUT_MS = 25000


def _build_summary_body(items: list[dict]) -> str:
    lines = ["StudyFlow Daily Summary", ""]
    for item in items:
        lines.append(
            f"- {item['course_name']} | {item['title']} "
            f"(Due: {item['due_date_str']})"
        )
        if item.get("draft_link"):
            lines.append(f"  Draft: {item['draft_link']}")
        if item.get("assignment_link"):
            lines.append(f"  Assignment: {item['assignment_link']}")
        lines.append("")

    lines.append("Review each draft, edit, then submit.")
    return "\n".join(lines)


async def _set_body_text(body_editor, text: str) -> bool:
    try:
        await body_editor.fill(text)
        return True
    except Exception:
        pass

    try:
        await body_editor.evaluate(
            """(el, value) => {
                const tag = (el.tagName || '').toLowerCase();
                if (tag === 'textarea' || tag === 'input') {
                    el.value = value;
                } else if (el.isContentEditable) {
                    el.textContent = value;
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


def _is_gmail_auth_page(page: Page) -> bool:
    url = (page.url or "").lower()
    return "accounts.google.com" in url or "service=mail" in url


async def _find_compose_button(page: Page):
    selectors = [
        '[gh="cm"]',
        '[role="button"][gh="cm"]',
        '[role="button"]:has-text("Compose")',
    ]
    for selector in selectors:
        try:
            btn = page.locator(selector).first
            if await btn.is_visible(timeout=5000):
                return btn
        except Exception:
            continue
    return None


async def send_daily_summary(items: list[dict], recipient_email: str = "") -> None:
    if not items:
        logger.info("No assignments processed, skipping email")
        return

    page = await new_page()

    try:
        await safe_goto(
            page,
            "https://mail.google.com/mail/u/0/#inbox",
            wait_selector=None,
            timeout=GMAIL_GOTO_TIMEOUT_MS,
        )
        await asyncio.sleep(1.5)

        if _is_gmail_auth_page(page):
            logger.warning("Gmail sign-in required for summary email; skipping")
            return

        # Click compose
        compose_btn = await _find_compose_button(page)
        if not compose_btn:
            logger.warning("Compose button not found in Gmail; skipping summary email")
            return
        await compose_btn.click(timeout=8000)
        await asyncio.sleep(0.8)

        # Fill To
        to_field = page.locator('[aria-label="To recipients"], [name="to"], input[aria-label*="To"]').first
        await to_field.click(timeout=8000)
        await to_field.fill(recipient_email or "me")
        await page.keyboard.press("Enter")
        await asyncio.sleep(0.2)

        # Fill Subject
        subject_field = page.locator('[name="subjectbox"], [aria-label="Subject"]').first
        await subject_field.click(timeout=8000)
        await subject_field.fill(f"StudyFlow: {len(items)} Draft(s) Ready for Review")
        await asyncio.sleep(0.1)

        # Fill Body
        body_editor = page.locator(
            '[role="textbox"][aria-label*="Body"], '
            '[contenteditable="true"][aria-label*="Body"], '
            '[aria-label="Message Body"]'
        ).first
        await body_editor.click(timeout=8000)
        await asyncio.sleep(0.1)

        body_text = _build_summary_body(items)
        body_ok = await _set_body_text(body_editor, body_text)
        if not body_ok:
            await page.keyboard.insert_text(body_text)

        await asyncio.sleep(0.3)
        send_btn = page.locator('[role="button"][aria-label*="Send"], [data-tooltip*="Send"]').first
        await send_btn.click(timeout=8000)
        await asyncio.sleep(1.0)

        logger.info("Summary email sent")

    except Exception:
        logger.exception("Failed to send email via Gmail")
    finally:
        await page.close()
