import asyncio
import logging

from browser.session import new_page, safe_goto

logger = logging.getLogger(__name__)


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


async def send_daily_summary(items: list[dict], recipient_email: str = "") -> None:
    if not items:
        logger.info("No assignments processed, skipping email")
        return

    page = await new_page()

    try:
        await safe_goto(page, "https://mail.google.com/mail/u/0/#inbox",
                        wait_selector='[role="button"]')
        await asyncio.sleep(4)

        # Click compose
        compose_btn = page.locator('[class*="T-I T-I-KE"], [role="button"]:has-text("Compose")').first
        await compose_btn.click(timeout=15000)
        await asyncio.sleep(1.5)

        # Fill To
        to_field = page.locator('[aria-label="To recipients"], [name="to"], input[aria-label*="To"]').first
        await to_field.click()
        await to_field.fill(recipient_email or "me")
        await page.keyboard.press("Enter")
        await asyncio.sleep(0.2)

        # Fill Subject
        subject_field = page.locator('[name="subjectbox"], [aria-label="Subject"]').first
        await subject_field.click()
        await subject_field.fill(f"StudyFlow: {len(items)} Draft(s) Ready for Review")
        await asyncio.sleep(0.1)

        # Fill Body
        body_editor = page.locator(
            '[role="textbox"][aria-label*="Body"], '
            '[contenteditable="true"][aria-label*="Body"], '
            '[aria-label="Message Body"]'
        ).first
        await body_editor.click()
        await asyncio.sleep(0.1)

        body_text = _build_summary_body(items)
        body_ok = await _set_body_text(body_editor, body_text)
        if not body_ok:
            await page.keyboard.insert_text(body_text)

        await asyncio.sleep(0.3)
        send_btn = page.locator('[role="button"][aria-label*="Send"], [data-tooltip*="Send"]').first
        await send_btn.click(timeout=15000)
        await asyncio.sleep(2)

        logger.info("Summary email sent")

    except Exception:
        logger.exception("Failed to send email via Gmail")
    finally:
        await page.close()
