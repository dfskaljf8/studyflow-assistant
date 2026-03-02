import asyncio
import logging
import random
import re
from dataclasses import dataclass

from playwright.async_api import Locator, Page

from browser.ap_session import ap_session_exists, close_ap_browser, get_ap_page
from browser.session import get_page
from config.settings import settings
from classroom.playwright_utils import (
    MOD_KEY,
    apply_default_timeouts,
    click_with_retry,
    fill_with_retry,
    goto_with_retry,
)
from classroom.scanner import Assignment
from classroom.submission_handler import (
    SmartFillResult,
    detect_assignment_question_fields,
    extract_doc_section_prompts,
    fill_detected_fields,
    fill_doc_sections,
    smart_fill_fields,
    summarize_attachment_context,
    _extract_editable_fields,
    _extract_questions_from_page,
)
from drafting.llm_drafter import generate_structured_answers

logger = logging.getLogger(__name__)

MAX_COMMENT_PASTE_CHARS = 6000
MAX_DOC_PASTE_CHARS = 20000
HUMAN_TYPING_MIN_DELAY_MS = 18
HUMAN_TYPING_MAX_DELAY_MS = 42
TEMPLATE_MAX_FIELDS = 24
MIN_TEMPLATE_PROMPTS = 2


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


def _is_ap_classroom_url(url: str) -> bool:
    lowered = (url or "").lower()
    return "collegeboard.org" in lowered and (
        "myap" in lowered or "apclassroom" in lowered or "digitalportfolio" in lowered
    )


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
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"^\s*#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"<\s*text\s*>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<\s*answer\s*>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\[\s*insert[^\]]*\]", "", text, flags=re.IGNORECASE)
    text = re.sub(
        r"^\s*\[\s*answer\s*\d*\s*\]\s*", "", text, flags=re.IGNORECASE | re.MULTILINE
    )
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
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
        await page.keyboard.press(f"{MOD_KEY}+A")
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
    logger.info("  Paste: navigating to assignment with retries")
    await goto_with_retry(page, assignment_url, wait_until="domcontentloaded")
    await asyncio.sleep(1.2)


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
    expected_id = assignment.assignment_id or _extract_assignment_id(
        assignment.assignment_url
    )
    current_url = (page.url or "").lower()
    current_title = ""

    try:
        current_title = (await _read_assignment_heading(page) or "").lower()
    except Exception:
        pass

    if expected_id:
        expected_id_lower = expected_id.lower()
        if expected_id_lower in current_url:
            return True
        if f"/a/{expected_id_lower}" in current_url:
            return True
        logger.warning(
            "Assignment ID mismatch. expected_id=%s, current_url=%s",
            expected_id,
            page.url,
        )

    if current_title and assignment.title:
        title_lower = assignment.title.lower()
        if _titles_match(assignment.title, current_title):
            return True
        if current_title in title_lower or title_lower in current_title:
            logger.info("Title partial match: '%s' vs '%s'", current_title, title_lower)
            return True

    logger.warning(
        "Assignment context mismatch. expected_id=%s, expected_title=%s, current_url=%s, heading=%s",
        expected_id,
        assignment.title,
        page.url,
        current_title,
    )
    return False


async def _open_and_verify_assignment(
    page: Page, assignment: Assignment, mark_skip_on_fail: bool = True
) -> bool:
    for attempt in range(1, 4):
        await _open_assignment_for_paste(page, assignment.assignment_url)
        if await _verify_assignment_context(page, assignment):
            return True
        if attempt < 3:
            logger.warning(
                "  Paste: assignment verification failed (attempt %d/3), retrying...",
                attempt,
            )
            await asyncio.sleep(1.5)

    if mark_skip_on_fail:
        assignment.delivery_method = "skipped_mismatch"
        assignment.delivery_details = "assignment_verification_failed"
    logger.warning(
        "Skipping delivery due to persistent assignment mismatch: %s", assignment.title
    )
    return False


async def _find_first_visible(
    page: Page, selectors: list[str], timeout_ms: int
) -> Locator | None:
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


def _to_google_doc_url(url: str) -> str:
    raw = (url or "").strip()
    cleaned = _strip_query(raw)
    if _is_google_doc_url(cleaned):
        return cleaned

    if "drive.google.com/file/d/" in raw or "drive.google.com/open?id=" in raw:
        return raw

    return ""


async def _collect_google_doc_links(page: Page, assignment: Assignment) -> list[str]:
    urls: list[str] = []

    for url in assignment.attachment_urls:
        converted = _to_google_doc_url(url)
        if converted:
            urls.append(converted)

    try:
        page_links = await page.evaluate(
            """
            () => {
                const out = [];

                const attrs = ['href', 'data-url', 'data-link', 'data-href', 'src', 'aria-label'];
                const els = document.querySelectorAll('[href], [data-url], [data-link], [data-href], [src], [aria-label]');

                for (const el of els) {
                    for (const attr of attrs) {
                        const value = (el.getAttribute(attr) || '').trim();
                        if (!value) continue;
                        if (
                            value.includes('docs.google.com/document/d/') ||
                            value.includes('drive.google.com/file/d/') ||
                            value.includes('drive.google.com/open?id=')
                        ) {
                            out.push(value);
                        }
                    }
                }

                const html = document.documentElement?.outerHTML || '';
                const re = new RegExp("https?://(?:docs\\.google\\.com/document/d/[^\"'\\s<]+|drive\\.google\\.com/file/d/[^\"'\\s<]+|drive\\.google\\.com/open\\?id=[^\"'\\s<]+)", "g");
                const matches = html.match(re) || [];
                for (const m of matches) {
                    out.push(m);
                }

                return out;
            }
            """
        )
        for url in page_links:
            converted = _to_google_doc_url(str(url))
            if converted:
                urls.append(converted)
    except Exception:
        pass

    unique: list[str] = []
    seen = set()
    for url in urls:
        if url and url not in seen:
            unique.append(url)
            seen.add(url)
    return unique


async def _collect_ap_classroom_links(page: Page, assignment: Assignment) -> list[str]:
    urls: list[str] = []

    for url in assignment.attachment_urls:
        cleaned = _strip_query(url)
        if _is_ap_classroom_url(cleaned):
            urls.append(cleaned)

    try:
        page_links = await page.evaluate(
            """
            () => {
                const out = [];
                const attrs = ['href', 'data-url', 'data-link', 'data-href'];
                const els = document.querySelectorAll('[href], [data-url], [data-link], [data-href]');

                for (const el of els) {
                    for (const attr of attrs) {
                        const value = (el.getAttribute(attr) || '').trim();
                        if (!value) continue;
                        if (value.includes('collegeboard.org') || value.includes('myap')) {
                            out.push(value);
                        }
                    }
                }

                const html = document.documentElement?.outerHTML || '';
                const re = new RegExp("https?://[^\"'\\s<]*collegeboard\\.org[^\"'\\s<]*", "g");
                const matches = html.match(re) || [];
                for (const m of matches) out.push(m);

                return out;
            }
            """
        )
        for url in page_links:
            cleaned = _strip_query(str(url))
            if _is_ap_classroom_url(cleaned):
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
        await goto_with_retry(page, doc_url, wait_until="domcontentloaded")
    except Exception as exc:
        logger.warning("  Paste: failed to open Google Doc %s: %s", doc_url, exc)
        return False

    try:
        await page.wait_for_load_state("networkidle", timeout=45000)
    except Exception:
        pass

    if "drive.google.com" in (page.url or ""):
        open_with = await _find_first_visible(
            page,
            [
                'a:has-text("Open with Google Docs")',
                'button:has-text("Open with Google Docs")',
                '[role="button"]:has-text("Open with Google Docs")',
                'a:has-text("Open in Docs")',
            ],
            timeout_ms=2500,
        )
        if open_with:
            try:
                await click_with_retry(open_with, timeout_ms=5000)
                await asyncio.sleep(2)
            except Exception:
                pass

    await asyncio.sleep(2)
    if _is_google_doc_url(page.url):
        return True

    logger.warning("  Paste: opened page is not a Docs URL: %s", page.url)
    return False


async def _doc_looks_view_only(page: Page) -> bool:
    try:
        return bool(
            await page.evaluate(
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
            )
        )
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
        await click_with_retry(target, timeout_ms=5000)
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
    q_count = len(
        re.findall(
            r"\b(question|prompt|response|answer)\b", doc_text, flags=re.IGNORECASE
        )
    )
    prompt_like = len(
        [
            ln
            for ln in doc_text.splitlines()
            if re.search(r"\?|:\s*$|_{3,}|\[\s*\]", ln.strip())
            and 4 <= len(ln.strip()) <= 180
        ]
    )
    return min(
        TEMPLATE_MAX_FIELDS,
        max(
            marker_count,
            min(labeled_lines, TEMPLATE_MAX_FIELDS),
            min(q_count, TEMPLATE_MAX_FIELDS),
            min(prompt_like, TEMPLATE_MAX_FIELDS),
        ),
    )


def _is_prompt_candidate(line: str) -> bool:
    if not (4 <= len(line) <= 200):
        return False
    if line.startswith("http://") or line.startswith("https://"):
        return False

    lower = line.lower()
    keyword_prompt = bool(
        re.search(
            r"\b(what|why|how|describe|explain|choose|list|write|source type|brief summary|your thoughts|free response|entry\s*#|title, author|topic)\b",
            lower,
        )
    )
    if keyword_prompt and len(line.split()) <= 26:
        return True

    return bool(
        re.search(r"\?|:\s*$|_{3,}|\[\s*\]|\(\s*\)", line)
        or re.match(r"^\s*(?:\d+[.)]|[A-Z][.)]|[-*])\s+", line)
    )


def _extract_template_prompts(doc_text: str) -> list[str]:
    prompts: list[str] = []
    seen: set[str] = set()

    for raw in doc_text.splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if not _is_prompt_candidate(line):
            continue

        compact = _normalize_text(line)
        if not compact or compact in seen:
            continue
        seen.add(compact)

        prompts.append(line)
        if len(prompts) >= TEMPLATE_MAX_FIELDS:
            break

    return prompts


def _extract_fillable_prompts(doc_text: str) -> list[str]:
    raw_lines = [re.sub(r"\s+", " ", ln).strip() for ln in doc_text.splitlines()]
    prompt_entries: list[tuple[int, str]] = []
    seen: set[str] = set()

    for idx, line in enumerate(raw_lines):
        if not _is_prompt_candidate(line):
            continue
        compact = _normalize_text(line)
        if not compact or compact in seen:
            continue
        seen.add(compact)
        prompt_entries.append((idx, line))
        if len(prompt_entries) >= TEMPLATE_MAX_FIELDS:
            break

    if not prompt_entries:
        return []

    fillable: list[str] = []
    for i, (idx, prompt) in enumerate(prompt_entries):
        next_idx = (
            prompt_entries[i + 1][0] if i + 1 < len(prompt_entries) else len(raw_lines)
        )
        segment = [ln for ln in raw_lines[idx + 1 : next_idx] if ln]

        if not segment:
            fillable.append(prompt)
            continue

        first = segment[0]
        first_lower = first.lower()
        if re.search(r"_{3,}|\[\s*\]|\(\s*\)", first):
            fillable.append(prompt)
            continue
        if re.search(r"\b(type here|your answer|response|answer)\b", first_lower):
            fillable.append(prompt)
            continue
        if first.startswith("(") and first.endswith(")"):
            fillable.append(prompt)
            continue

    if fillable and len(fillable) >= max(2, len(prompt_entries) // 2):
        return fillable
    return [prompt for _, prompt in prompt_entries]


def _looks_like_shared_class_table(doc_text: str) -> bool:
    """Detect shared class spreadsheet/table docs (not personal templates).
    These have many student names / repeated row patterns."""
    if not doc_text:
        return False
    lines = doc_text.splitlines()
    tab_lines = sum(1 for ln in lines if "\t" in ln)
    if tab_lines < 5:
        return False
    block_mentions = len(re.findall(r"\bblock\s*\d", doc_text, flags=re.IGNORECASE))
    name_like = len(re.findall(r"\b[A-Z][a-z]+\s+[A-Z][a-z]+\b", doc_text))
    return block_mentions >= 4 or name_like >= 8


def _student_data_already_present(doc_text: str, student_name: str) -> bool:
    """Check if the student's name is already in the doc content."""
    if not student_name or not doc_text:
        return False
    return student_name.lower() in doc_text.lower()


def _looks_like_template(doc_text: str) -> bool:
    if not doc_text:
        return False
    if _looks_like_shared_class_table(doc_text):
        return False
    fields = _estimate_template_fields(doc_text)
    prompts = extract_doc_section_prompts(doc_text, max_fields=TEMPLATE_MAX_FIELDS)
    return fields >= MIN_TEMPLATE_PROMPTS or len(prompts) >= MIN_TEMPLATE_PROMPTS


def _strip_answer_label(text: str) -> str:
    out = text.strip()
    out = re.sub(r"^\s*\[\s*answer\s*\d*\s*\]\s*", "", out, flags=re.IGNORECASE)
    out = re.sub(
        r"^\s*(?:answer|response)\s*\d*\s*[:.-]\s*", "", out, flags=re.IGNORECASE
    )
    return out.strip()


def _split_template_answers(text: str, max_fields: int) -> list[str]:
    if not text.strip():
        return []

    marker_matches = list(re.finditer(r"(?im)^\s*\[\s*answer\s*(\d+)\s*\]\s*$", text))
    if marker_matches:
        pieces: list[str] = []
        for i, match in enumerate(marker_matches):
            start = match.end()
            end = (
                marker_matches[i + 1].start()
                if i + 1 < len(marker_matches)
                else len(text)
            )
            chunk = _strip_answer_label(text[start:end])
            if chunk:
                pieces.append(chunk)
        if pieces:
            blocks = pieces
        else:
            blocks = [text.strip()]
    else:
        blocks = [
            chunk.strip() for chunk in re.split(r"\n\s*\n+", text) if chunk.strip()
        ]

    if len(blocks) <= 1 and max_fields > 1:
        blocks = [
            chunk.strip()
            for chunk in re.split(r"\n(?=\s*(?:\d+[.)]|[-*]))", text)
            if chunk.strip()
        ]

    blocks = [_strip_answer_label(b) for b in blocks if _strip_answer_label(b)]

    if not blocks:
        return [text.strip()] if text.strip() else []

    if max_fields > 0 and len(blocks) > max_fields:
        kept = blocks[: max_fields - 1]
        kept.append("\n\n".join(blocks[max_fields - 1 :]))
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
                    delay=random.randint(
                        HUMAN_TYPING_MIN_DELAY_MS, HUMAN_TYPING_MAX_DELAY_MS
                    ),
                )
                buffer.clear()
                burst_size = random.randint(2, 6)

                if random.random() < 0.25:
                    await asyncio.sleep(random.uniform(0.08, 0.35))

        if buffer:
            await page.keyboard.type(
                "".join(buffer),
                delay=random.randint(
                    HUMAN_TYPING_MIN_DELAY_MS, HUMAN_TYPING_MAX_DELAY_MS
                ),
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
        await page.keyboard.press(f"{MOD_KEY}+ArrowUp")
    except Exception:
        pass


async def _go_to_doc_end(page: Page) -> None:
    try:
        await page.keyboard.press(f"{MOD_KEY}+ArrowDown")
    except Exception:
        pass


async def _jump_to_marker(page: Page, marker: str) -> bool:
    """Use Find to locate marker text in document, then navigate to type below it."""
    try:
        await page.keyboard.press(f"{MOD_KEY}+f")
    except Exception:
        return False

    await asyncio.sleep(0.2)
    try:
        await page.keyboard.press(f"{MOD_KEY}+A")
        await asyncio.sleep(0.06)
        await page.keyboard.type(marker, delay=25)
        await asyncio.sleep(0.15)
        await page.keyboard.press("Enter")
        await asyncio.sleep(0.25)
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.1)
        return True
    except Exception:
        return False


async def _jump_to_question_number(page: Page, question_num: int) -> bool:
    """Find a numbered question (e.g., '1.', '1)', '2.', '2)') and position cursor at the answer area below it."""
    markers_to_try = [f"{question_num}.", f"{question_num})", f"{question_num}."]

    for marker in markers_to_try:
        try:
            await page.keyboard.press(f"{MOD_KEY}+f")
        except Exception:
            continue

        await asyncio.sleep(0.25)
        try:
            await page.keyboard.press(f"{MOD_KEY}+A")
            await asyncio.sleep(0.08)
            await page.keyboard.type(marker, delay=20)
            await asyncio.sleep(0.2)
            await page.keyboard.press("Enter")
            await asyncio.sleep(0.25)
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.15)
            return True
        except Exception:
            continue
    return False

    await asyncio.sleep(0.2)
    try:
        await page.keyboard.press(f"{MOD_KEY}+A")
        await asyncio.sleep(0.06)
        await page.keyboard.type(marker, delay=25)
        await asyncio.sleep(0.15)
        await page.keyboard.press("Enter")
        await asyncio.sleep(0.2)
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.15)
        await page.keyboard.press("ArrowRight")
        await asyncio.sleep(0.1)
        return True
    except Exception:
        return False


async def _jump_to_question_number(page: Page, question_num: int) -> bool:
    """Find a numbered question (e.g., '1.', '2.') and position cursor at the answer area below it."""
    marker = f"{question_num}."
    try:
        await page.keyboard.press(f"{MOD_KEY}+f")
    except Exception:
        return False

    await asyncio.sleep(0.25)
    try:
        await page.keyboard.press(f"{MOD_KEY}+A")
        await asyncio.sleep(0.08)
        await page.keyboard.type(marker, delay=20)
        await asyncio.sleep(0.2)
        await page.keyboard.press("Enter")
        await asyncio.sleep(0.25)
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.15)
        await page.keyboard.press("ArrowRight")
        await asyncio.sleep(0.1)
        await page.keyboard.press("ArrowDown")
        await asyncio.sleep(0.1)
        await page.keyboard.press("Home")
        await asyncio.sleep(0.1)
        return True
    except Exception:
        return False


async def _focus_first_template_field(page: Page) -> None:
    for marker in [
        "___",
        "[ ]",
        "answer",
        "response",
        "claim",
        "reasoning",
        "type here",
    ]:
        if await _jump_to_marker(page, marker):
            return

    await _go_to_doc_start(page)


@dataclass
class _DocTableInfo:
    is_table: bool = False
    layout: str = (
        ""  # "side" = answer box beside prompt, "below" = answer box under prompt
    )


async def _detect_doc_table_layout(page: Page, doc_id: str) -> _DocTableInfo:
    """Detect if the doc uses a table layout and whether answer boxes are beside or below prompts."""
    result = _DocTableInfo()
    if not doc_id:
        return result
    html_url = f"https://docs.google.com/document/d/{doc_id}/export?format=html"
    try:
        resp = await page.context.request.get(html_url, timeout=15000)
        if not resp.ok:
            await resp.dispose()
            return result
        html = await resp.text()
        await resp.dispose()
        import re as _re

        tables = _re.findall(r"<table[^>]*>.*?</table>", html, _re.DOTALL)
        if not tables:
            return result

        for table_html in tables:
            rows = _re.findall(r"<tr[^>]*>(.*?)</tr>", table_html, _re.DOTALL)
            if len(rows) < 2:
                continue

            # Check for side-by-side layout: prompt in left cell, empty right cell
            side_rows = 0
            for row in rows:
                cells = _re.findall(r"<td[^>]*>(.*?)</td>", row, _re.DOTALL)
                if len(cells) == 2:
                    left = _re.sub(r"<[^>]+>", "", cells[0]).strip()
                    right = _re.sub(r"<[^>]+>", "", cells[1]).strip()
                    if left and not right:
                        side_rows += 1
            if side_rows >= 2:
                result.is_table = True
                result.layout = "side"
                return result

            # Check for stacked layout: prompt row then empty answer row (single-column or wide cell)
            below_pairs = 0
            for i in range(len(rows) - 1):
                cells_this = _re.findall(r"<td[^>]*>(.*?)</td>", rows[i], _re.DOTALL)
                cells_next = _re.findall(
                    r"<td[^>]*>(.*?)</td>", rows[i + 1], _re.DOTALL
                )
                this_text = " ".join(
                    _re.sub(r"<[^>]+>", "", c).strip() for c in cells_this
                )
                next_text = " ".join(
                    _re.sub(r"<[^>]+>", "", c).strip() for c in cells_next
                )
                if this_text and not next_text:
                    below_pairs += 1
            if below_pairs >= 2:
                result.is_table = True
                result.layout = "below"
                return result

        return result
    except Exception:
        return result


async def _type_answer_into_adjacent_cell(page: Page, prompt: str, answer: str) -> bool:
    """For table-layout docs (side): Find the prompt, then Tab into the adjacent answer cell.
    NEVER deletes existing content -- only types into the cell."""
    prompt_query = re.sub(r"\s+", " ", prompt).strip()
    if len(prompt_query) > 90:
        prompt_query = prompt_query[:90]
    if not prompt_query:
        return False

    found = await _jump_to_marker(page, prompt_query)
    if not found:
        return False

    # Close Find bar, then Tab to adjacent cell
    try:
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.15)
    except Exception:
        pass

    await page.keyboard.press("Tab")
    await asyncio.sleep(0.25)

    await _human_type_text(page, answer)
    await asyncio.sleep(0.35)
    return True


async def _type_answer_into_cell_below(page: Page, prompt: str, answer: str) -> bool:
    """For table-layout docs (below): Find the prompt, then move down into the answer cell beneath."""
    prompt_query = re.sub(r"\s+", " ", prompt).strip()
    if len(prompt_query) > 90:
        prompt_query = prompt_query[:90]
    if not prompt_query:
        return False

    found = await _jump_to_marker(page, prompt_query)
    if not found:
        return False

    try:
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.15)
    except Exception:
        pass

    # Move to end of prompt cell, then down into the answer cell below
    await page.keyboard.press("End")
    await asyncio.sleep(0.08)
    await page.keyboard.press("ArrowDown")
    await asyncio.sleep(0.2)

    await _human_type_text(page, answer)
    await asyncio.sleep(0.35)
    return True


async def _type_answer_under_prompt(page: Page, prompt: str, answer: str) -> bool:
    """For non-table docs: Find the prompt, move to the empty area below it, then type.
    NEVER deletes existing content -- only adds text where the cursor lands."""
    prompt_query = re.sub(r"\s+", " ", prompt).strip()
    if len(prompt_query) > 90:
        prompt_query = prompt_query[:90]

    if not prompt_query:
        return False

    found = await _jump_to_marker(page, prompt_query)
    if not found:
        return False

    try:
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.1)
    except Exception:
        pass

    await page.keyboard.press("End")
    await asyncio.sleep(0.08)
    await page.keyboard.press("ArrowDown")
    await asyncio.sleep(0.1)
    await page.keyboard.press("Home")
    await asyncio.sleep(0.08)
    await page.keyboard.press("Enter")
    await asyncio.sleep(0.1)

    await _human_type_text(page, answer)
    await asyncio.sleep(0.2)
    await page.keyboard.press("Space")
    await asyncio.sleep(0.1)
    return True


async def _fill_blank_on_line(page: Page, prompt: str, answer: str) -> bool:
    """For fill-in-the-blank: find the underscores near the prompt and replace with the answer word."""
    prompt_query = re.sub(r"\s+", " ", prompt).strip()
    if len(prompt_query) > 90:
        prompt_query = prompt_query[:90]
    if not prompt_query:
        return False

    found = await _jump_to_marker(page, prompt_query)
    if not found:
        return False

    try:
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.15)
    except Exception:
        pass

    # Find the underscores on this line
    blank_found = await _jump_to_marker(page, "______")
    if blank_found:
        try:
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.1)
        except Exception:
            pass
        # Select the underscores and a bit more to cover the full blank
        # Use Shift+End to select to end of underscores, then type over
        await page.keyboard.press("Home")
        await asyncio.sleep(0.05)
        # Use Find+Replace approach: find underscores, they get selected, type over
        await page.keyboard.press(f"{MOD_KEY}+h")
        await asyncio.sleep(0.3)
        # Type underscores pattern in find field
        await page.keyboard.type("______", delay=20)
        await asyncio.sleep(0.15)
        await page.keyboard.press("Tab")
        await asyncio.sleep(0.1)
        await page.keyboard.type(answer, delay=25)
        await asyncio.sleep(0.15)
        # Replace just this one
        # Look for replace button
        replace_btn = await page.query_selector('[aria-label="Replace"]')
        if replace_btn:
            await replace_btn.click()
            await asyncio.sleep(0.3)
        # Close find+replace
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.2)
        return True

    # Fallback: just go to end of line and type
    await page.keyboard.press("End")
    await asyncio.sleep(0.08)
    await page.keyboard.type(f" {answer}", delay=30)
    await asyncio.sleep(0.2)
    return True


async def _highlight_mc_answer(page: Page, prompt: str, answer: str) -> bool:
    """For multiple choice: find the question, then type the answer in the space below it."""
    num_match = re.match(r"^(\d+)", prompt.strip())
    if not num_match:
        num_match = re.match(r"^(\d+)", answer.strip())

    question_num = 1
    if num_match:
        question_num = int(num_match.group(1))

    found = await _jump_to_question_number(page, question_num)
    if not found:
        return False

    await _human_type_text(page, answer)
    await asyncio.sleep(0.2)
    await page.keyboard.press("Space")
    await asyncio.sleep(0.1)
    logger.info("  Paste: MC answer typed: %s", answer[:50])
    return True


async def _place_answer_by_type(
    page: Page, prompt: str, answer: str, q_type: str, table_info: _DocTableInfo
) -> bool:
    """Route answer placement based on question type and doc layout."""
    if q_type == "fill_blank":
        return await _fill_blank_on_line(page, prompt, answer)
    elif q_type == "multiple_choice":
        return await _highlight_mc_answer(page, prompt, answer)
    else:
        # free_response: use table-aware or standard placement
        if table_info.is_table and table_info.layout == "side":
            return await _type_answer_into_adjacent_cell(page, prompt, answer)
        elif table_info.is_table and table_info.layout == "below":
            return await _type_answer_into_cell_below(page, prompt, answer)
        else:
            return await _type_answer_under_prompt(page, prompt, answer)


async def _fill_template_fields(
    page: Page,
    assignment: Assignment,
    doc_snapshot: str,
    style_examples: list[str],
    material_texts: list[str],
) -> bool:
    prompts = extract_doc_section_prompts(doc_snapshot, max_fields=TEMPLATE_MAX_FIELDS)
    if len(prompts) < MIN_TEMPLATE_PROMPTS:
        logger.warning("  Paste: template-like doc has insufficient section prompts")
        return False

    # Detect table layout from the doc
    doc_url = _strip_query(page.url or "")
    doc_id = _extract_doc_id(doc_url)
    table_info = await _detect_doc_table_layout(page, doc_id)

    attachment_summary = summarize_attachment_context(
        assignment.attachment_urls, material_texts
    )
    answers = generate_structured_answers(
        assignment=assignment,
        style_examples=style_examples,
        material_texts=material_texts,
        question_snippets=prompts,
        attachment_summary=attachment_summary,
    )

    logger.info(
        "  Paste: filling template (%d prompts, %d answers, table=%s/%s)",
        len(prompts),
        len(answers),
        table_info.is_table,
        table_info.layout,
    )

    # Go to doc start before filling
    await _go_to_doc_start(page)
    await asyncio.sleep(0.3)

    filled_count = 0
    for i, ans_dict in enumerate(answers):
        q = ans_dict.get("question", "")
        a = ans_dict.get("answer", "")
        q_type = ans_dict.get("question_type", "free_response")
        if not a:
            continue

        logger.info(
            "  Paste: placing answer %d/%d (type=%s)", i + 1, len(answers), q_type
        )
        success = await _place_answer_by_type(page, q, a, q_type, table_info)
        if success:
            filled_count += 1
            await asyncio.sleep(0.3)
        else:
            logger.warning("  Paste: failed to place answer %d for: %s", i + 1, q[:50])

    return filled_count > 0


async def _paste_into_google_doc(
    page: Page,
    assignment: Assignment,
    text: str,
    style_examples: list[str],
    material_texts: list[str],
) -> bool:
    if await _doc_looks_view_only(page):
        logger.info("  Paste: Google Doc appears view-only")
        return False

    try:
        await page.wait_for_load_state("networkidle", timeout=45000)
    except Exception:
        pass

    try:
        await page.wait_for_selector(
            "div.kix-appview-editor, iframe.docs-texteventtarget-iframe",
            state="visible",
            timeout=60000,
        )
    except Exception:
        logger.warning("  Paste: doc editor did not become visible in time")
        return False

    focused = await _focus_doc_editor(page)
    if not focused:
        logger.warning("  Paste: could not focus Google Doc editor")
        return False

    try:
        doc_snapshot = await _read_doc_snapshot(page)

        # Skip shared class tables where the student already has an entry
        if _looks_like_shared_class_table(doc_snapshot):
            # Extract student name from the assignment or use "Kushal Surepalli" as fallback
            student_name = "Kushal Surepalli"
            if _student_data_already_present(doc_snapshot, student_name):
                logger.info(
                    "  Paste: shared class table doc already contains student data, skipping"
                )
                return False
            logger.info(
                "  Paste: shared class table doc detected but student data missing; appending to end"
            )
            await _go_to_doc_end(page)
            await asyncio.sleep(0.3)
            await page.keyboard.press("Enter")
            await asyncio.sleep(0.1)
            await _human_type_text(page, text)
            await asyncio.sleep(0.8)
            return True

        if _looks_like_template(doc_snapshot):
            logger.info("  Paste: template-like doc detected, filling fields")
            filled = await _fill_template_fields(
                page=page,
                assignment=assignment,
                doc_snapshot=doc_snapshot,
                style_examples=style_examples,
                material_texts=material_texts,
            )
            if filled:
                await asyncio.sleep(0.8)
                return True
            logger.warning("  Paste: template fill failed for template-like doc")
            return False

        # Regular doc: go to end and type
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
        await goto_with_retry(page, copy_url, wait_until="domcontentloaded")
    except Exception as exc:
        logger.warning("  Paste: failed to open copy page: %s", exc)
        return ""

    try:
        await page.wait_for_load_state("networkidle", timeout=45000)
    except Exception:
        pass

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
            await fill_with_retry(
                name_input, f"{assignment_title} - StudyFlow", timeout_ms=5000
            )
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
            await click_with_retry(make_copy_button, timeout_ms=5000)
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


async def _attach_doc_link_to_assignment(
    page: Page, assignment: Assignment, doc_url: str
) -> bool:
    logger.info("  Paste: attaching copied Google Doc in assignment")
    if not await _open_and_verify_assignment(page, assignment, mark_skip_on_fail=False):
        return False

    try:
        await page.wait_for_load_state("networkidle", timeout=45000)
    except Exception:
        pass

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
        await click_with_retry(add_or_create, timeout_ms=5000)
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
        await click_with_retry(link_option, timeout_ms=5000)
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
        await fill_with_retry(url_input, doc_url, timeout_ms=5000)
    except Exception:
        try:
            await click_with_retry(url_input, timeout_ms=3000)
            await page.keyboard.press(f"{MOD_KEY}+A")
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
            await click_with_retry(add_link_button, timeout_ms=5000)
        else:
            await page.keyboard.press("Enter")
    except Exception:
        logger.warning("  Paste: failed confirming link attach")
        return False

    await asyncio.sleep(1.5)
    return True


def _title_overlap_score(assignment_title: str, doc_snapshot: str) -> int:
    title_tokens = {
        tok for tok in _normalize_text(assignment_title).split() if len(tok) > 3
    }
    if not title_tokens:
        return 0
    snapshot_tokens = set(_normalize_text(doc_snapshot[:6000]).split())
    return len(title_tokens & snapshot_tokens)


async def _rank_doc_candidates(
    page: Page, assignment: Assignment, doc_links: list[str]
) -> list[str]:
    scored: list[tuple[int, str]] = []
    for doc_url in doc_links:
        if not await _open_google_doc(page, doc_url):
            continue
        snapshot = await _read_doc_snapshot(page)
        score = 0
        if _looks_like_template(snapshot):
            score += 10
        score += _estimate_template_fields(snapshot)
        score += _title_overlap_score(assignment.title, snapshot)
        scored.append((score, doc_url))

    scored.sort(key=lambda item: item[0], reverse=True)
    return [doc_url for _, doc_url in scored]


async def _deliver_via_google_doc(
    page: Page,
    assignment: Assignment,
    text: str,
    style_examples: list[str],
    material_texts: list[str],
) -> bool:
    doc_links = await _collect_google_doc_links(page, assignment)
    if not doc_links:
        logger.warning("  Paste: no attached Google Doc links found")
        return False

    logger.info("  Paste: found %d attached Google Doc link(s)", len(doc_links))

    ordered_links = await _rank_doc_candidates(page, assignment, doc_links)
    if not ordered_links:
        ordered_links = doc_links

    for idx, doc_url in enumerate(ordered_links, start=1):
        logger.info(
            "  Paste: trying ranked attached Doc %d/%d", idx, len(ordered_links)
        )
        if not await _open_google_doc(page, doc_url):
            continue

        view_only = await _doc_looks_view_only(page)
        if not view_only and await _paste_into_google_doc(
            page=page,
            assignment=assignment,
            text=text,
            style_examples=style_examples,
            material_texts=material_texts,
        ):
            assignment.delivery_method = "doc_edited"
            assignment.delivery_details = _strip_query(doc_url)
            logger.info(
                "Draft pasted into attached Google Doc for: %s", assignment.title
            )
            return True

        if not view_only:
            logger.warning(
                "  Paste: attached doc is editable but template fill failed; not creating a new doc"
            )
            continue

        logger.info(
            "  Paste: attached doc appears view-only; trying copy + attach fallback"
        )

        copied_doc_url = await _make_doc_copy(page, doc_url, assignment.title)
        if not copied_doc_url:
            continue

        if not await _open_google_doc(page, copied_doc_url):
            continue

        if not await _paste_into_google_doc(
            page=page,
            assignment=assignment,
            text=text,
            style_examples=style_examples,
            material_texts=material_texts,
        ):
            continue

        attached = await _attach_doc_link_to_assignment(
            page, assignment, copied_doc_url
        )
        if not attached:
            logger.warning(
                "  Paste: copied Google Doc was filled, but attachment step failed"
            )
            continue

        assignment.delivery_method = "doc_copy_attached"
        assignment.delivery_details = copied_doc_url
        logger.info(
            "Draft pasted into copied Google Doc and attached for: %s", assignment.title
        )
        return True

    return False


async def _deliver_via_ap_classroom(
    page: Page,
    assignment: Assignment,
    style_examples: list[str],
    material_texts: list[str],
) -> bool:
    ap_links = await _collect_ap_classroom_links(page, assignment)
    if not ap_links:
        return False

    if not ap_session_exists():
        logger.warning(
            "  Paste: AP session not set up yet. Run: python main.py ap-login"
        )
        return False

    logger.info(
        "  Paste: found %d AP Classroom link(s), using dedicated AP session",
        len(ap_links),
    )

    try:
        ap_page = await get_ap_page()
    except Exception as exc:
        logger.warning("  Paste: could not open AP browser session: %s", exc)
        return False

    for idx, ap_url in enumerate(ap_links, start=1):
        logger.info("  Paste: trying AP Classroom link %d/%d", idx, len(ap_links))
        try:
            await goto_with_retry(ap_page, ap_url, wait_until="domcontentloaded")
        except Exception as exc:
            logger.warning(
                "  Paste: failed to open AP Classroom link %s: %s", ap_url, exc
            )
            continue

        try:
            await ap_page.wait_for_load_state("networkidle", timeout=60000)
        except Exception:
            pass

        url_now = (ap_page.url or "").lower()
        if "login" in url_now or "signin" in url_now:
            logger.warning("  Paste: AP session expired. Run: python main.py ap-login")
            return False

        try:
            detected = await _extract_editable_fields(ap_page)
        except Exception as exc:
            logger.info("  Paste: AP Classroom field detection unavailable: %s", exc)
            continue

        if not detected:
            logger.info("  Paste: no fillable AP Classroom fields found on link")
            continue

        questions = []
        try:
            questions = await _extract_questions_from_page(ap_page)
        except Exception:
            pass

        question_snippets = (
            [q.snippet for q in questions]
            if questions
            else [f.nearby_text for f in detected]
        )
        attachment_summary = summarize_attachment_context(
            assignment.attachment_urls + [ap_url],
            material_texts,
        )
        answers = generate_structured_answers(
            assignment=assignment,
            style_examples=style_examples,
            material_texts=material_texts,
            question_snippets=question_snippets,
            attachment_summary=attachment_summary,
        )

        debug_dir = str(settings.project_root)
        try:
            result: SmartFillResult = await smart_fill_fields(
                ap_page, answers, debug_dir=debug_dir
            )
        except Exception as exc:
            logger.warning("  Paste: AP smart_fill_fields failed: %s", exc)
            continue

        if result.filled_count <= 0:
            continue

        assignment.delivery_method = "ap_classroom_fields_filled"
        assignment.delivery_details = f"{_strip_query(ap_url)} | fields_filled={result.filled_count}/{result.total_fields}"
        logger.info(
            "SmartFill: filled %d AP Classroom field(s) for: %s",
            result.filled_count,
            assignment.title,
        )
        return True

    return False


async def _deliver_via_assignment_fields(
    page: Page,
    assignment: Assignment,
    style_examples: list[str],
    material_texts: list[str],
) -> bool:
    # Use the new smart_fill_fields flow: scroll, extract, LLM, fill per-field
    try:
        detected = await _extract_editable_fields(page)
    except Exception as exc:
        logger.info("  Paste: assignment field detection unavailable: %s", exc)
        return False

    if not detected:
        return False

    questions = []
    try:
        questions = await _extract_questions_from_page(page)
    except Exception:
        pass

    question_snippets = (
        [q.snippet for q in questions]
        if questions
        else [f.nearby_text for f in detected]
    )
    attachment_summary = summarize_attachment_context(
        assignment.attachment_urls, material_texts
    )
    answers = generate_structured_answers(
        assignment=assignment,
        style_examples=style_examples,
        material_texts=material_texts,
        question_snippets=question_snippets,
        attachment_summary=attachment_summary,
    )

    logger.info(
        "  Paste: SmartFill flow — %d field(s), %d question(s), %d answer(s)",
        len(detected),
        len(question_snippets),
        len(answers),
    )

    debug_dir = str(settings.project_root)
    try:
        result: SmartFillResult = await smart_fill_fields(
            page, answers, debug_dir=debug_dir
        )
    except Exception as exc:
        logger.warning("  Paste: smart_fill_fields failed: %s", exc)
        return False

    if result.filled_count <= 0:
        logger.warning(
            "  Paste: SmartFill filled 0 fields (fallback=%s)", result.fallback_used
        )
        return False

    assignment.delivery_method = "classroom_fields_filled"
    assignment.delivery_details = f"fields_filled={result.filled_count}/{result.total_fields} fallback={result.fallback_used}"
    logger.info(
        "SmartFill: filled %d/%d Classroom field(s) for: %s (fallback=%s)",
        result.filled_count,
        result.total_fields,
        assignment.title,
        result.fallback_used,
    )
    return True


async def paste_draft(
    assignment: Assignment,
    draft_text: str,
    style_examples: list[str] | None = None,
    material_texts: list[str] | None = None,
) -> bool:
    assignment.delivery_method = "failed"
    assignment.delivery_details = ""

    if not assignment.assignment_url:
        logger.warning("No URL for: %s", assignment.title)
        assignment.delivery_method = "failed"
        assignment.delivery_details = "missing_assignment_url"
        return False

    logger.info("  Paste: acquiring shared tab")
    page = await get_page()
    apply_default_timeouts(page)
    logger.info("  Paste: shared tab ready")
    doc_text = _prepare_draft_for_doc(draft_text)
    style_examples = style_examples or []
    material_texts = material_texts or []

    try:
        logger.info("  Paste: opening assignment page")
        if not await _open_and_verify_assignment(
            page, assignment, mark_skip_on_fail=True
        ):
            return False

        try:
            await page.wait_for_load_state("networkidle", timeout=45000)
        except Exception:
            pass

        delivered_via_fields = await _deliver_via_assignment_fields(
            page=page,
            assignment=assignment,
            style_examples=style_examples,
            material_texts=material_texts,
        )
        if delivered_via_fields:
            return True

        delivered_via_doc = await _deliver_via_google_doc(
            page=page,
            assignment=assignment,
            text=doc_text,
            style_examples=style_examples,
            material_texts=material_texts,
        )
        if delivered_via_doc:
            return True

        delivered_via_ap = await _deliver_via_ap_classroom(
            page=page,
            assignment=assignment,
            style_examples=style_examples,
            material_texts=material_texts,
        )
        if delivered_via_ap:
            return True

        assignment.delivery_method = "failed"
        assignment.delivery_details = "doc_and_ap_delivery_failed"
        logger.warning("Google Doc/AP delivery failed for: %s", assignment.title)
        return False

    except Exception:
        logger.exception("Paste failed for: %s", assignment.title)
        assignment.delivery_method = "failed"
        assignment.delivery_details = "exception"
        return False
