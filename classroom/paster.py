import asyncio
import logging
import random
import re

from playwright.async_api import Locator, Page

from browser.session import get_page
from classroom.scanner import Assignment

logger = logging.getLogger(__name__)

MAX_COMMENT_PASTE_CHARS = 6000
MAX_DOC_PASTE_CHARS = 20000
HUMAN_TYPING_MIN_DELAY_MS = 18
HUMAN_TYPING_MAX_DELAY_MS = 42
TEMPLATE_MAX_FIELDS = 8


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", value.lower())).strip()


def _titles_match(expected: str, observed: str) -> bool:
    exp = _normalize_text(expected)
    obs = _normalize_text(observed)
    if not exp or not obs:
        return False
    if exp in obs or obs in exp:
        return True

    exp_tokens = {t for t in exp.split() if len(t) > 2}
    obs_tokens = {t for t in obs.split() if len(t) > 2}
    if not exp_tokens or not obs_tokens:
        return False

    overlap = len(exp_tokens & obs_tokens)
    return overlap >= min(2, len(exp_tokens))


def _strip_query(url: str) -> str:
    return url.split("?", 1)[0].split("#", 1)[0]


def _extract_assignment_id(url: str) -> str:
    match = re.search(r"/a/([^/?#]+)", url or "")
    return match.group(1) if match else ""


def _extract_doc_id(url: str) -> str:
    match = re.search(r"/document/d/([^/?#]+)", url or "")
    return match.group(1) if match else ""


def _is_google_doc_url(url: str) -> bool:
    return "docs.google.com/document/d/" in (url or "")


def _prepare_draft_for_comment(draft_text: str) -> str:
    text = draft_text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(text) > MAX_COMMENT_PASTE_CHARS:
        logger.warning(
            "Draft too long for reliable browser paste (%d chars). Truncating to %d chars.",
            len(text),
            MAX_COMMENT_PASTE_CHARS,
        )
        return text[:MAX_COMMENT_PASTE_CHARS]
    return text


def _prepare_draft_for_doc(draft_text: str) -> str:
    text = draft_text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(text) > MAX_DOC_PASTE_CHARS:
        logger.warning(
            "Draft is very long for Docs typing (%d chars). Truncating to %d chars.",
            len(text),
            MAX_DOC_PASTE_CHARS,
        )
        return text[:MAX_DOC_PASTE_CHARS]
    return text


async def _fast_paste_into_editor(page: Page, editor: Locator, text: str) -> bool:
    try:
        await editor.fill(text, timeout=5000)
        return True
    except Exception:
        pass

    try:
        await editor.click(timeout=5000)
        await page.keyboard.press("Meta+A")
        await page.keyboard.insert_text(text)
        return True
    except Exception:
        pass

    try:
        handle = await editor.element_handle()
        if not handle:
            return False
        ok = await handle.evaluate(
            """
            (el, value) => {
                if (el instanceof HTMLTextAreaElement || el instanceof HTMLInputElement) {
                    el.focus();
                    el.value = value;
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    return true;
                }
                if (el.isContentEditable) {
                    el.focus();
                    el.innerText = value;
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    return true;
                }
                return false;
            }
            """,
            text,
        )
        return bool(ok)
    except Exception:
        return False


async def _open_assignment_for_paste(page: Page, assignment_url: str) -> None:
    logger.info("  Paste: navigating with wait_until=commit")
    try:
        await page.goto(assignment_url, wait_until="commit", timeout=12000)
        logger.info("  Paste: navigation succeeded (commit)")
    except Exception as exc:
        logger.warning("  Paste: navigation failed (commit): %s", exc)
        try:
            logger.info("  Paste: JS navigation fallback")
            await page.evaluate("(url) => { window.location.assign(url); }", assignment_url)
        except Exception as js_exc:
            logger.warning("  Paste: JS fallback failed: %s", js_exc)

    await asyncio.sleep(2)


async def _read_assignment_heading(page: Page) -> str:
    selectors = [
        '[role="main"] h1',
        'div[role="heading"][aria-level="1"]',
        "h1",
    ]
    for selector in selectors:
        loc = page.locator(selector)
        try:
            count = await loc.count()
        except Exception:
            continue
        for idx in range(min(3, count)):
            try:
                item = loc.nth(idx)
                if await item.is_visible(timeout=800):
                    text = (await item.inner_text(timeout=1000)).strip()
                    if text:
                        return text
            except Exception:
                continue
    return ""


async def _verify_assignment_context(page: Page, assignment: Assignment) -> bool:
    expected_id = assignment.assignment_id or _extract_assignment_id(assignment.assignment_url)
    current_url = page.url or ""
    if expected_id:
        if f"/a/{expected_id}" in current_url:
            return True
        logger.warning(
            "Assignment ID mismatch. expected_id=%s, current_url=%s",
            expected_id,
            current_url,
        )
        return False

    heading = await _read_assignment_heading(page)
    if heading and _titles_match(assignment.title, heading):
        return True

    logger.warning(
        "Assignment context mismatch. expected_id=%s, expected_title=%s, current_url=%s, heading=%s",
        expected_id,
        assignment.title,
        current_url,
        heading,
    )
    return False


async def _open_and_verify_assignment(page: Page, assignment: Assignment, mark_skip_on_fail: bool = True) -> bool:
    for attempt in range(1, 3):
        await _open_assignment_for_paste(page, assignment.assignment_url)
        if await _verify_assignment_context(page, assignment):
            return True
        if attempt == 1:
            logger.warning("  Paste: assignment verification failed, retrying navigation once")
            await asyncio.sleep(1)

    if mark_skip_on_fail:
        assignment.delivery_method = "skipped_mismatch"
        assignment.delivery_details = "assignment_verification_failed"
    logger.warning("Skipping delivery due to persistent assignment mismatch: %s", assignment.title)
    return False


async def _find_first_visible(page: Page, selectors: list[str], timeout_ms: int) -> Locator | None:
    if not selectors:
        return None

    per_selector_timeout = max(250, int(timeout_ms / len(selectors)))
    for selector in selectors:
        loc = page.locator(selector)
        try:
            count = await loc.count()
        except Exception:
            continue

        max_candidates = min(count, 8)
        for idx in range(max_candidates):
            try:
                candidate = loc.nth(idx)
                await candidate.wait_for(state="visible", timeout=per_selector_timeout)
                return candidate
            except Exception:
                continue
    return None


async def _collect_google_doc_links(page: Page, assignment: Assignment) -> list[str]:
    urls: list[str] = []

    for url in assignment.attachment_urls:
        cleaned = _strip_query(url)
        if _is_google_doc_url(cleaned):
            urls.append(cleaned)

    try:
        page_links = await page.evaluate(
            """
            () => {
                const out = [];
                for (const a of document.querySelectorAll('a[href*="docs.google.com/document/d/"]')) {
                    const href = (a.href || '').split('?')[0];
                    if (href) out.push(href);
                }
                return out;
            }
            """
        )
        for url in page_links:
            cleaned = _strip_query(str(url))
            if _is_google_doc_url(cleaned):
                urls.append(cleaned)
    except Exception:
        pass

    unique: list[str] = []
    seen = set()
    for url in urls:
        if url and url not in seen:
            unique.append(url)
            seen.add(url)
    return unique


async def _open_google_doc(page: Page, doc_url: str) -> bool:
    logger.info("  Paste: opening Google Doc attachment")
    try:
        await page.goto(doc_url, wait_until="commit", timeout=15000)
    except Exception:
        try:
            await page.goto(doc_url, wait_until="domcontentloaded", timeout=20000)
        except Exception as exc:
            logger.warning("  Paste: failed to open Google Doc %s: %s", doc_url, exc)
            return False

    await asyncio.sleep(2)
    if _is_google_doc_url(page.url):
        return True

    logger.warning("  Paste: opened page is not a Docs URL: %s", page.url)
    return False


async def _doc_looks_view_only(page: Page) -> bool:
    try:
        return bool(await page.evaluate(
            """
            () => {
                const text = (document.body?.innerText || '').toLowerCase();
                const markers = [
                    'view only',
                    'request edit access',
                    'you need access',
                    "can't edit",
                    'cannot edit',
                    'ask the owner'
                ];
                return markers.some(m => text.includes(m));
            }
            """
        ))
    except Exception:
        return False


async def _focus_doc_editor(page: Page) -> bool:
    selectors = [
        "div.kix-appview-editor",
        "div.kix-canvas-tile-content",
        "iframe.docs-texteventtarget-iframe",
    ]

    target = await _find_first_visible(page, selectors, timeout_ms=4000)
    if not target:
        return False

    try:
        await target.click(timeout=2000)
    except Exception:
        return False
    return True


def _normalize_doc_text(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized.strip()


def _estimate_template_fields(doc_text: str) -> int:
    if not doc_text:
        return 0

    marker_count = len(re.findall(r"_{3,}|\[\s*\]|\(\s*\)", doc_text))
    labeled_lines = sum(
        1
        for ln in doc_text.splitlines()
        if ln.strip().endswith(":") and 2 <= len(ln.strip()) <= 80
    )
    q_count = len(re.findall(r"\b(question|prompt|response|answer)\b", doc_text, flags=re.IGNORECASE))
    return min(TEMPLATE_MAX_FIELDS, max(marker_count, min(labeled_lines, TEMPLATE_MAX_FIELDS), min(q_count, TEMPLATE_MAX_FIELDS)))


def _looks_like_template(doc_text: str) -> bool:
    if not doc_text:
        return False
    fields = _estimate_template_fields(doc_text)
    return fields >= 2


def _split_template_answers(text: str, max_fields: int) -> list[str]:
    blocks = [chunk.strip() for chunk in re.split(r"\n\s*\n+", text) if chunk.strip()]
    if len(blocks) <= 1 and max_fields > 1:
        blocks = [
            chunk.strip()
            for chunk in re.split(r"\n(?=\s*(?:\d+[.)]|[-*]))", text)
            if chunk.strip()
        ]

    if not blocks:
        return [text.strip()] if text.strip() else []

    if max_fields > 0 and len(blocks) > max_fields:
        kept = blocks[: max_fields - 1]
        kept.append("\n\n".join(blocks[max_fields - 1:]))
        return kept

    return blocks


async def _human_type_text(page: Page, text: str) -> None:
    lines = text.split("\n")
    for line_idx, line in enumerate(lines):
        words = line.split(" ")
        buffer: list[str] = []
        burst_size = random.randint(2, 6)

        for idx, word in enumerate(words):
            piece = word
            if idx < len(words) - 1:
                piece += " "
            buffer.append(piece)

            if len(buffer) >= burst_size:
                await page.keyboard.type(
                    "".join(buffer),
                    delay=random.randint(HUMAN_TYPING_MIN_DELAY_MS, HUMAN_TYPING_MAX_DELAY_MS),
                )
                buffer.clear()
                burst_size = random.randint(2, 6)

                if random.random() < 0.25:
                    await asyncio.sleep(random.uniform(0.08, 0.35))

        if buffer:
            await page.keyboard.type(
                "".join(buffer),
                delay=random.randint(HUMAN_TYPING_MIN_DELAY_MS, HUMAN_TYPING_MAX_DELAY_MS),
            )

        if line_idx < len(lines) - 1:
            await page.keyboard.press("Enter")
            await asyncio.sleep(random.uniform(0.06, 0.2))


async def _read_doc_snapshot(page: Page) -> str:
    doc_url = _strip_query(page.url or "")
    doc_id = _extract_doc_id(doc_url)

    if doc_id:
        export_url = f"https://docs.google.com/document/d/{doc_id}/export?format=txt"
        try:
            response = await page.context.request.get(export_url, timeout=15000)
            if response.ok:
                text = await response.text()
                cleaned = _normalize_doc_text(text)
                if cleaned:
                    await response.dispose()
                    return cleaned
            await response.dispose()
        except Exception:
            pass

    try:
        text = await page.evaluate("() => document.body?.innerText || ''")
        return _normalize_doc_text(text)
    except Exception:
        return ""


async def _go_to_doc_start(page: Page) -> None:
    try:
        await page.keyboard.press("Meta+ArrowUp")
        return
    except Exception:
        pass
    try:
        await page.keyboard.press("Control+Home")
    except Exception:
        pass


async def _go_to_doc_end(page: Page) -> None:
    try:
        await page.keyboard.press("Meta+ArrowDown")
        return
    except Exception:
        pass
    try:
        await page.keyboard.press("Control+End")
    except Exception:
        pass


async def _jump_to_marker(page: Page, marker: str) -> bool:
    try:
        await page.keyboard.press("Meta+f")
    except Exception:
        try:
            await page.keyboard.press("Control+f")
        except Exception:
            return False

    await asyncio.sleep(0.2)
    try:
        await page.keyboard.type(marker, delay=25)
        await asyncio.sleep(0.15)
        await page.keyboard.press("Enter")
        await asyncio.sleep(0.2)
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.2)
        return True
    except Exception:
        return False


async def _focus_first_template_field(page: Page) -> None:
    for marker in ["___", "[ ]", "answer", "response", "claim", "reasoning", "type here"]:
        if await _jump_to_marker(page, marker):
            return

    await _go_to_doc_start(page)


async def _fill_template_fields(page: Page, text: str, doc_snapshot: str) -> bool:
    field_count = _estimate_template_fields(doc_snapshot)
    answers = _split_template_answers(text, max_fields=field_count or TEMPLATE_MAX_FIELDS)
    if not answers:
        return False

    await _focus_first_template_field(page)
    typed_any = False

    for idx, answer in enumerate(answers):
        if not answer.strip():
            continue
        await _human_type_text(page, answer.strip())
        typed_any = True

        if idx < len(answers) - 1:
            try:
                await page.keyboard.press("Tab")
                await asyncio.sleep(random.uniform(0.12, 0.35))
            except Exception:
                try:
                    await page.keyboard.press("Enter")
                    await asyncio.sleep(random.uniform(0.12, 0.3))
                except Exception:
                    pass

    return typed_any


async def _paste_into_google_doc(page: Page, text: str) -> bool:
    if await _doc_looks_view_only(page):
        logger.info("  Paste: Google Doc appears view-only")
        return False

    focused = await _focus_doc_editor(page)
    if not focused:
        logger.warning("  Paste: could not focus Google Doc editor")
        return False

    try:
        doc_snapshot = await _read_doc_snapshot(page)
        if _looks_like_template(doc_snapshot):
            logger.info("  Paste: template-like doc detected, filling fields")
            filled = await _fill_template_fields(page, text, doc_snapshot)
            if filled:
                await asyncio.sleep(0.8)
                return True
            logger.warning("  Paste: template fill could not be confirmed, falling back to append mode")

        if doc_snapshot:
            await _go_to_doc_end(page)
            await asyncio.sleep(0.2)
            await page.keyboard.press("Enter")
            await asyncio.sleep(0.1)

        await _human_type_text(page, text)
        await asyncio.sleep(0.8)
        return True
    except Exception as exc:
        logger.warning("  Paste: failed typing into Google Doc: %s", exc)
        return False


async def _make_doc_copy(page: Page, source_doc_url: str, assignment_title: str) -> str:
    source_id = _extract_doc_id(source_doc_url)
    if not source_id:
        return ""

    copy_url = f"https://docs.google.com/document/d/{source_id}/copy"
    logger.info("  Paste: creating Google Doc copy")

    try:
        await page.goto(copy_url, wait_until="domcontentloaded", timeout=20000)
    except Exception as exc:
        logger.warning("  Paste: failed to open copy page: %s", exc)
        return ""

    await asyncio.sleep(1.2)

    name_input = await _find_first_visible(
        page,
        [
            'input[aria-label="Name"]',
            'input[aria-label*="name"]',
            'input[type="text"]',
        ],
        timeout_ms=2500,
    )
    if name_input:
        try:
            await name_input.fill(f"{assignment_title} - StudyFlow", timeout=2000)
        except Exception:
            pass

    make_copy_button = await _find_first_visible(
        page,
        [
            'button:has-text("Make a copy")',
            'button:has-text("Copy document")',
            '[role="button"]:has-text("Make a copy")',
            '[role="button"]:has-text("Copy")',
            'button:has-text("OK")',
        ],
        timeout_ms=3500,
    )
    if make_copy_button:
        try:
            await make_copy_button.click(timeout=2500)
        except Exception:
            pass
    else:
        try:
            await page.keyboard.press("Enter")
        except Exception:
            pass

    try:
        await page.wait_for_url("**/document/d/**", timeout=20000)
    except Exception:
        pass

    await asyncio.sleep(1.0)
    copied_url = _strip_query(page.url or "")
    copied_id = _extract_doc_id(copied_url)

    if copied_id and copied_id != source_id:
        return copied_url

    logger.warning("  Paste: could not confirm copied Google Doc URL")
    return ""


async def _attach_doc_link_to_assignment(page: Page, assignment: Assignment, doc_url: str) -> bool:
    logger.info("  Paste: attaching copied Google Doc in assignment")
    if not await _open_and_verify_assignment(page, assignment, mark_skip_on_fail=False):
        return False

    add_or_create = await _find_first_visible(
        page,
        [
            'button:has-text("Add or create")',
            '[role="button"]:has-text("Add or create")',
            'div[role="button"]:has-text("Add or create")',
        ],
        timeout_ms=3500,
    )
    if not add_or_create:
        logger.warning("  Paste: could not find 'Add or create'")
        return False

    try:
        await add_or_create.click(timeout=2000)
    except Exception:
        logger.warning("  Paste: failed clicking 'Add or create'")
        return False

    await asyncio.sleep(0.8)

    link_option = await _find_first_visible(
        page,
        [
            '[role="menuitem"]:has-text("Link")',
            'li:has-text("Link")',
            'span:has-text("Link")',
            'button:has-text("Link")',
        ],
        timeout_ms=3000,
    )
    if not link_option:
        logger.warning("  Paste: could not find Link option in Add or create menu")
        return False

    try:
        await link_option.click(timeout=2000)
    except Exception:
        logger.warning("  Paste: failed opening Link dialog")
        return False

    await asyncio.sleep(0.8)

    url_input = await _find_first_visible(
        page,
        [
            'div[role="dialog"] input[type="text"]',
            'div[role="dialog"] input',
            'input[type="url"]',
            'input[type="text"]',
            'input[aria-label*="Link"]',
            'input[placeholder*="link"]',
            'input[placeholder*="URL"]',
        ],
        timeout_ms=3500,
    )
    if not url_input:
        logger.warning("  Paste: link URL input not found")
        return False

    try:
        await url_input.fill(doc_url, timeout=2500)
    except Exception:
        try:
            await url_input.click(timeout=1500)
            await page.keyboard.press("Meta+A")
            await page.keyboard.insert_text(doc_url)
        except Exception:
            logger.warning("  Paste: failed filling link URL")
            return False

    add_link_button = await _find_first_visible(
        page,
        [
            'button:has-text("Add link")',
            '[role="button"]:has-text("Add link")',
            'button:has-text("Add")',
            '[role="button"]:has-text("Add")',
        ],
        timeout_ms=3000,
    )

    try:
        if add_link_button:
            await add_link_button.click(timeout=2000)
        else:
            await page.keyboard.press("Enter")
    except Exception:
        logger.warning("  Paste: failed confirming link attach")
        return False

    await asyncio.sleep(1.5)
    return True


async def _deliver_via_google_doc(page: Page, assignment: Assignment, text: str) -> bool:
    doc_links = await _collect_google_doc_links(page, assignment)
    if not doc_links:
        logger.info("  Paste: no attached Google Doc found")
        return False

    logger.info("  Paste: found %d attached Google Doc link(s)", len(doc_links))

    for idx, doc_url in enumerate(doc_links, start=1):
        logger.info("  Paste: trying attached Doc %d/%d", idx, len(doc_links))
        if not await _open_google_doc(page, doc_url):
            continue

        if await _paste_into_google_doc(page, text):
            assignment.delivery_method = "doc_edited"
            assignment.delivery_details = _strip_query(doc_url)
            logger.info("Draft pasted into attached Google Doc for: %s", assignment.title)
            return True

        copied_doc_url = await _make_doc_copy(page, doc_url, assignment.title)
        if not copied_doc_url:
            continue

        if not await _open_google_doc(page, copied_doc_url):
            continue

        if not await _paste_into_google_doc(page, text):
            continue

        attached = await _attach_doc_link_to_assignment(page, assignment, copied_doc_url)
        if not attached:
            logger.warning("  Paste: copied Google Doc was filled, but attachment step failed")
            continue

        assignment.delivery_method = "doc_copy_attached"
        assignment.delivery_details = copied_doc_url
        logger.info("Draft pasted into copied Google Doc and attached for: %s", assignment.title)
        return True

    return False


async def _draft_private_comment(page: Page, text: str, title: str) -> bool:
    logger.info("  Paste: trying private comment fallback")

    comment_input_selectors = [
        'textarea[aria-label*="Private comment"]',
        'textarea[placeholder*="Private comment"]',
        'textarea[aria-label*="comment"]',
        'textarea[placeholder*="comment"]',
        'div[role="textbox"][aria-label*="Private comment"]',
        'div[role="textbox"][data-placeholder*="Private comment"]',
        'div[role="textbox"][aria-label*="comment"]',
        'div[role="textbox"][data-placeholder*="comment"]',
    ]

    comment_input = await _find_first_visible(page, comment_input_selectors, timeout_ms=1800)
    if not comment_input:
        reveal_selectors = [
            'text=Add comment to',
            'button:has-text("Private comments")',
            'button:has-text("Add private comment")',
            'a:has-text("Add comment to")',
            'span:has-text("Add comment to")',
            '[aria-label*="Private comments"]',
        ]
        reveal = await _find_first_visible(page, reveal_selectors, timeout_ms=1200)
        if reveal:
            try:
                await reveal.click(timeout=2000)
                await asyncio.sleep(0.8)
                logger.info("  Paste: opened private comment input")
            except Exception:
                pass
            comment_input = await _find_first_visible(page, comment_input_selectors, timeout_ms=1800)

    if not comment_input:
        logger.warning("Private comment box not found for: %s", title)
        return False

    entered = await _fast_paste_into_editor(page, comment_input, text)
    if not entered:
        try:
            await page.keyboard.insert_text(text)
            entered = True
        except Exception:
            entered = False

    if not entered:
        logger.warning("Could not enter private comment text for: %s", title)
        return False

    logger.info("Private comment drafted (not posted) for: %s", title)
    return True


async def paste_draft(assignment: Assignment, draft_text: str) -> bool:
    assignment.delivery_method = "failed"
    assignment.delivery_details = ""

    if not assignment.assignment_url:
        logger.warning("No URL for: %s", assignment.title)
        assignment.delivery_method = "failed"
        assignment.delivery_details = "missing_assignment_url"
        return False

    logger.info("  Paste: acquiring shared tab")
    page = await get_page()
    logger.info("  Paste: shared tab ready")
    doc_text = _prepare_draft_for_doc(draft_text)
    comment_text = _prepare_draft_for_comment(draft_text)

    try:
        logger.info("  Paste: opening assignment page")
        if not await _open_and_verify_assignment(page, assignment, mark_skip_on_fail=True):
            return False

        delivered_via_doc = await _deliver_via_google_doc(page, assignment, doc_text)
        if delivered_via_doc:
            return True

        if not await _open_and_verify_assignment(page, assignment, mark_skip_on_fail=False):
            assignment.delivery_method = "skipped_mismatch"
            assignment.delivery_details = "fallback_assignment_verification_failed"
            return False

        delivered_via_comment = await _draft_private_comment(page, comment_text, assignment.title)
        if delivered_via_comment:
            assignment.delivery_method = "comment_drafted"
            assignment.delivery_details = "private_comment_draft_not_posted"
            return True

        assignment.delivery_method = "failed"
        assignment.delivery_details = "no_delivery_target_found"
        logger.warning("No delivery input found for: %s", assignment.title)
        return False

    except Exception:
        logger.exception("Paste failed for: %s", assignment.title)
        assignment.delivery_method = "failed"
        assignment.delivery_details = "exception"
        return False
