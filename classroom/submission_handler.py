import asyncio
import json
import logging
import random
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Awaitable, Callable

from playwright.async_api import Locator, Page
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_fixed

from classroom.playwright_utils import MOD_KEY, click_with_retry, fill_with_retry

logger = logging.getLogger(__name__)

EDITABLE_FIELD_SELECTOR = (
    'textarea:not([aria-label*="Search" i]):not([placeholder*="Search" i]):not([aria-hidden="true"]), '
    'div[contenteditable="true"]:not([aria-hidden="true"]), '
    'input[type="text"]:not([aria-label*="Search" i]):not([placeholder*="Search" i]):not([name*="search" i]):not([aria-hidden="true"]), '
    '[role="textbox"]:not([aria-label*="Search" i]):not([placeholder*="Search" i]):not([aria-hidden="true"]), '
    '.ql-editor'
)

IGNORE_RE = re.compile(
    r"\b(turn in|add class comment|private comment|stream|class comment|search|instructions?|submit)\b",
    flags=re.IGNORECASE,
)

QUESTION_MARKER_RE = re.compile(r"\?|:\s*$|_{3,}|\[\s*\]|\(\s*\)")


@dataclass
class DetectedQuestion:
    index: int
    snippet: str


@dataclass
class DetectedField:
    index: int
    field_id: str
    tag: str
    role: str
    input_type: str
    nearby_text: str


@dataclass
class SmartFillResult:
    total_fields: int = 0
    filled_count: int = 0
    failed_count: int = 0
    fallback_used: bool = False
    screenshots: list[str] = field(default_factory=list)


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", (value or "").lower())).strip()


def summarize_attachment_context(attachment_urls: list[str], material_texts: list[str], max_chars: int = 2200) -> str:
    lines: list[str] = []
    if attachment_urls:
        lines.append("Attachment URLs:")
        for i, url in enumerate(attachment_urls[:12], start=1):
            lines.append(f"{i}. {url}")
    snippets = [m.strip() for m in material_texts if m and m.strip()]
    if snippets:
        lines.append("")
        lines.append("Attachment text snippets:")
        for i, text in enumerate(snippets[:4], start=1):
            lines.append(f"[{i}] {text[:500]}")
    summary = "\n".join(lines).strip()
    return summary[:max_chars]


def extract_doc_section_prompts(doc_text: str, max_fields: int = 24) -> list[str]:
    prompts: list[str] = []
    seen: set[str] = set()
    for raw in (doc_text or "").splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if not (4 <= len(line) <= 260):
            continue
        lower = line.lower()
        if IGNORE_RE.search(lower):
            continue
        prompt_like = bool(
            QUESTION_MARKER_RE.search(line)
            or re.match(r"^\s*(?:\d+[.)]|[A-Z][.)]|[-*])\s+", line)
            or re.search(r"\b(what|why|how|describe|explain|list|choose|brief summary|source type|free response)\b", lower)
        )
        if not prompt_like:
            continue
        norm = normalize_text(line)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        prompts.append(line)
        if len(prompts) >= max_fields:
            break
    return prompts


# ---------------------------------------------------------------------------
#  Phase 1 -- Slow-scroll + extract questions + editable fields
# ---------------------------------------------------------------------------

async def _slow_scroll_page(page: Page) -> None:
    try:
        total_height = await page.evaluate("() => document.body.scrollHeight")
        viewport_h = await page.evaluate("() => window.innerHeight")
        step = max(200, viewport_h // 2)
        pos = 0
        while pos < total_height:
            pos += step
            await page.evaluate(f"window.scrollTo(0, {pos})")
            await asyncio.sleep(random.uniform(0.15, 0.35))
        await page.evaluate("window.scrollTo(0, 0)")
        await asyncio.sleep(0.3)
    except Exception:
        pass


async def _extract_questions_from_page(page: Page) -> list[DetectedQuestion]:
    """Extract question-like text elements in DOM order."""
    raw = await page.evaluate("""
    () => {
        const clean = (t) => (t || '').replace(/\\s+/g, ' ').trim();
        const ignore = /(turn in|add class comment|private comment|stream|search|submit|instructions?)/i;
        const qLike = (t) => {
            if (!t || t.length < 4 || t.length > 500) return false;
            if (ignore.test(t)) return false;
            return /\\?|:\\s*$|_{3,}|\\[\\s*\\]|\\(\\s*\\)/.test(t)
                || /^\\s*(?:\\d+[.)]|[A-Z][.)]|[-*])\\s+/.test(t)
                || /\\b(what|why|how|describe|explain|list|choose|write|brief summary|your (answer|response|thoughts)|free response)\\b/i.test(t);
        };
        const seen = new Set();
        const out = [];
        const tags = 'h1,h2,h3,h4,h5,label,p,span,div[role="heading"],legend';
        const els = Array.from(document.querySelectorAll(tags));
        for (const el of els) {
            const rect = el.getBoundingClientRect();
            const style = getComputedStyle(el);
            if (style.display === 'none' || style.visibility === 'hidden') continue;
            if (rect.width < 5 || rect.height < 5) continue;
            const t = clean(el.textContent || el.innerText);
            if (!qLike(t)) continue;
            const key = t.toLowerCase().slice(0, 120);
            if (seen.has(key)) continue;
            seen.add(key);
            out.push({ snippet: t.slice(0, 300), y: rect.top });
        }
        out.sort((a, b) => a.y - b.y);
        return out;
    }
    """)
    return [DetectedQuestion(index=i, snippet=r["snippet"]) for i, r in enumerate(raw)]


async def _extract_editable_fields(page: Page) -> list[DetectedField]:
    """Find all visible editable fields in DOM order and tag each with a unique id."""
    raw = await page.evaluate("""
    (selector) => {
        const clean = (t) => (t || '').replace(/\\s+/g, ' ').trim();
        const isVisible = (el) => {
            const r = el.getBoundingClientRect();
            const s = getComputedStyle(el);
            if (s.display === 'none' || s.visibility === 'hidden' || Number(s.opacity || 1) === 0) return false;
            return r.width > 6 && r.height > 6 && r.bottom > 0 && r.right > 0;
        };
        const ignore = /(turn in|add class comment|private comment|stream|search|submit)/i;
        const results = [];
        const nodes = Array.from(document.querySelectorAll(selector));
        let idx = 0;
        for (const el of nodes) {
            if (!isVisible(el)) continue;
            if (el.closest('[aria-hidden="true"]')) continue;
            const ariaLabel = clean(el.getAttribute('aria-label'));
            const placeholder = clean(el.getAttribute('placeholder'));
            const combo = ariaLabel + ' ' + placeholder;
            if (ignore.test(combo)) continue;

            idx += 1;
            const id = `sf-field-${Date.now()}-${idx}`;
            el.setAttribute('data-sf-field-id', id);

            // grab closest question text above this field
            const rect = el.getBoundingClientRect();
            let nearText = ariaLabel || placeholder || '';
            if (!nearText) {
                const container = el.closest('form, article, section, li, div[role="listitem"], [role="main"]') || el.parentElement;
                if (container) {
                    const cands = container.querySelectorAll('h1,h2,h3,h4,label,p,span,div[role="heading"],legend');
                    let best = null;
                    let bestDist = 9999;
                    for (const c of cands) {
                        if (c === el || c.contains(el) || el.contains(c)) continue;
                        const cR = c.getBoundingClientRect();
                        if (cR.bottom > rect.top + 18) continue;
                        if (Math.abs(cR.left - rect.left) > 700) continue;
                        const dist = Math.max(0, rect.top - cR.bottom);
                        if (dist < bestDist) {
                            bestDist = dist;
                            best = clean(c.textContent || c.innerText);
                        }
                    }
                    if (best && best.length >= 3 && best.length <= 300) nearText = best;
                }
            }
            if (!nearText) {
                let prev = el.previousElementSibling;
                let hops = 0;
                while (prev && hops < 3) {
                    const t = clean(prev.textContent || prev.innerText);
                    if (t && t.length >= 3 && t.length <= 300 && !ignore.test(t)) { nearText = t; break; }
                    prev = prev.previousElementSibling;
                    hops++;
                }
            }
            results.push({
                field_id: id,
                tag: (el.tagName || '').toLowerCase(),
                role: el.getAttribute('role') || '',
                input_type: el.getAttribute('type') || '',
                near: nearText || ('Field ' + idx),
                y: rect.top,
            });
        }
        results.sort((a, b) => a.y - b.y);
        return results;
    }
    """, EDITABLE_FIELD_SELECTOR)

    return [
        DetectedField(
            index=i,
            field_id=r["field_id"],
            tag=r["tag"],
            role=r.get("role", ""),
            input_type=r.get("input_type", ""),
            nearby_text=r.get("near", f"Field {i+1}"),
        )
        for i, r in enumerate(raw)
    ]


# ---------------------------------------------------------------------------
#  Phase 2 -- Match LLM answers to fields
# ---------------------------------------------------------------------------

def _similarity(a: str, b: str) -> float:
    na = normalize_text(a)
    nb = normalize_text(b)
    if not na or not nb:
        return 0.0
    if na in nb or nb in na:
        return 1.0
    ratio = SequenceMatcher(None, na, nb).ratio()
    at = set(na.split())
    bt = set(nb.split())
    overlap = len(at & bt) / max(1, len(at | bt))
    return ratio * 0.65 + overlap * 0.35


def _clean_answer(text: str) -> str:
    out = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    out = re.sub(r"^\s*```(?:json)?\s*", "", out, flags=re.IGNORECASE)
    out = re.sub(r"\s*```\s*$", "", out)
    out = re.sub(r"^\s*\[\s*answer\s*\d*\s*\]\s*", "", out, flags=re.IGNORECASE | re.MULTILINE)
    out = re.sub(r"\*\*(.+?)\*\*", r"\1", out)
    out = re.sub(r"__(.+?)__", r"\1", out)
    out = re.sub(r"`([^`]+)`", r"\1", out)
    out = re.sub(r"^\s*#{1,6}\s*", "", out, flags=re.MULTILINE)
    out = re.sub(r"<\s*/?\s*(?:text|answer)\s*>", "", out, flags=re.IGNORECASE)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def match_answers_to_fields(
    fields: list[DetectedField],
    answers: list[dict],
) -> list[tuple[DetectedField, str, float]]:
    """Return (field, answer_text, score) in field order. Positional fallback if scores are low."""
    cleaned: list[tuple[int, str, str]] = []
    for i, item in enumerate(answers):
        q = str(item.get("question") or item.get("question_snippet") or "").strip()
        a = _clean_answer(str(item.get("answer") or ""))
        if a:
            cleaned.append((i, q, a))

    if not cleaned:
        return [(f, "", 0.0) for f in fields]

    results: list[tuple[DetectedField, str, float]] = []
    used: set[int] = set()

    for fi, fld in enumerate(fields):
        best_idx = -1
        best_score = -1.0
        best_text = ""

        for ai, aq, at in cleaned:
            if ai in used:
                continue
            score = _similarity(fld.nearby_text, aq)
            if score > best_score:
                best_score = score
                best_idx = ai
                best_text = at

        if best_idx >= 0 and best_score >= 0.15:
            used.add(best_idx)
            results.append((fld, best_text, best_score))
        else:
            positional = fi if fi < len(cleaned) else len(cleaned) - 1
            ai, _, at = cleaned[positional]
            results.append((fld, at, 0.0))

    return results


# ---------------------------------------------------------------------------
#  Phase 3 -- Per-field fill with screenshot + human typing
# ---------------------------------------------------------------------------

async def _clear_field(page: Page, locator: Locator) -> None:
    try:
        tag = await locator.evaluate("el => el.tagName.toLowerCase()")
    except Exception:
        tag = ""

    if tag in ("textarea", "input"):
        try:
            await fill_with_retry(locator, "", timeout_ms=12_000)
            return
        except Exception:
            pass

    await click_with_retry(locator, timeout_ms=12_000)
    await page.keyboard.press(f"{MOD_KEY}+A")
    await asyncio.sleep(0.05)
    await page.keyboard.press("Backspace")
    await asyncio.sleep(0.05)


async def _human_type(page: Page, text: str, min_ms: int = 20, max_ms: int = 48) -> None:
    for li, line in enumerate(text.split("\n")):
        words = line.split(" ")
        buf: list[str] = []
        burst = random.randint(2, 5)
        for wi, word in enumerate(words):
            piece = word + (" " if wi < len(words) - 1 else "")
            buf.append(piece)
            if len(buf) >= burst:
                await page.keyboard.type("".join(buf), delay=random.randint(min_ms, max_ms))
                buf.clear()
                burst = random.randint(2, 5)
                if random.random() < 0.2:
                    await asyncio.sleep(random.uniform(0.06, 0.25))
        if buf:
            await page.keyboard.type("".join(buf), delay=random.randint(min_ms, max_ms))
        if li < len(text.split("\n")) - 1:
            await page.keyboard.press("Enter")
            await asyncio.sleep(random.uniform(0.04, 0.12))


async def _fill_single_field(
    page: Page,
    fld: DetectedField,
    answer_text: str,
    debug_dir: str | None = None,
    field_num: int = 0,
) -> bool:
    loc = page.locator(f'[data-sf-field-id="{fld.field_id}"]').first

    try:
        await loc.scroll_into_view_if_needed(timeout=8000)
    except Exception:
        pass
    await asyncio.sleep(0.15)

    try:
        await click_with_retry(loc, timeout_ms=12_000)
    except Exception as exc:
        logger.warning("  SmartFill: could not click field %d (%s): %s", field_num, fld.nearby_text[:60], exc)
        return False

    await _clear_field(page, loc)

    if fld.tag in ("textarea", "input"):
        try:
            await fill_with_retry(loc, answer_text, timeout_ms=15_000)
        except Exception:
            await _human_type(page, answer_text)
    else:
        await _human_type(page, answer_text)

    if debug_dir:
        try:
            path = f"{debug_dir}/debug_fill_{field_num}.png"
            await page.screenshot(path=path, full_page=False)
        except Exception:
            pass

    return True


# ---------------------------------------------------------------------------
#  Phase 4 -- Fallback: dump all answers into first visible field
# ---------------------------------------------------------------------------

async def _fallback_fill_first_field(
    page: Page,
    fields: list[DetectedField],
    combined_text: str,
) -> bool:
    if not fields:
        return False

    fld = fields[0]
    logger.warning("  SmartFill FALLBACK: dumping combined answer into first field (%s)", fld.nearby_text[:60])
    return await _fill_single_field(page, fld, combined_text, field_num=0)


# ---------------------------------------------------------------------------
#  Public API -- smart_fill_fields (the main entry point)
# ---------------------------------------------------------------------------

@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_fixed(2),
    retry=retry_if_exception_type((Exception,)),
)
async def smart_fill_fields(
    page: Page,
    answers: list[dict],
    debug_dir: str | None = None,
) -> SmartFillResult:
    """
    Robust multi-field filler.
    `answers` is a list of {"index": int, "question_snippet": str, "answer": str}.
    """
    result = SmartFillResult()

    # Wait for full page load + extra settle time
    try:
        await page.wait_for_load_state("networkidle", timeout=45000)
    except Exception:
        pass
    await asyncio.sleep(5)

    # Slow-scroll to force lazy-loaded fields into the DOM
    await _slow_scroll_page(page)

    # Extract fields
    fields = await _extract_editable_fields(page)
    result.total_fields = len(fields)

    logger.info("  SmartFill: detected %d editable field(s)", len(fields))
    for i, f in enumerate(fields):
        logger.info("  SmartFill: field %d [%s] nearby='%s'", i, f.tag, f.nearby_text[:80])

    if not fields:
        logger.warning("  SmartFill: no editable fields found on page")
        return result

    if not answers:
        logger.warning("  SmartFill: no answers provided")
        return result

    # Match answers to fields
    matches = match_answers_to_fields(fields, answers)

    # Fill each field in order
    for idx, (fld, answer_text, score) in enumerate(matches):
        if not answer_text.strip():
            logger.warning("  SmartFill: empty answer for field %d (%s), skipping", idx, fld.nearby_text[:60])
            result.failed_count += 1
            continue

        ok = await _fill_single_field(page, fld, answer_text, debug_dir=debug_dir, field_num=idx)
        if ok:
            result.filled_count += 1
            logger.info(
                "  SmartFill: filled field %d/%d | nearby='%s' | score=%.2f | answer_len=%d",
                idx + 1, len(fields), fld.nearby_text[:60], score, len(answer_text),
            )
        else:
            result.failed_count += 1
            logger.warning("  SmartFill: FAILED field %d (%s)", idx, fld.nearby_text[:60])

        await asyncio.sleep(random.uniform(0.8, 1.8))

    # If no fields were filled, try fallback
    if result.filled_count == 0 and answers:
        combined = "\n\n".join(
            _clean_answer(str(a.get("answer", "")))
            for a in answers
            if str(a.get("answer", "")).strip()
        )
        if combined.strip():
            ok = await _fallback_fill_first_field(page, fields, combined)
            if ok:
                result.filled_count = 1
                result.fallback_used = True

    return result


# ---------------------------------------------------------------------------
#  Legacy wrappers (kept for backward compat with paster.py doc-section flow)
# ---------------------------------------------------------------------------

@dataclass
class QuestionField:
    question_text_snippet: str
    locator_css: str
    tag: str = ""
    role: str = ""
    input_type: str = ""


async def detect_assignment_question_fields(page: Page) -> list[QuestionField]:
    """Legacy wrapper -- extracts fields and returns QuestionField list."""
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=12000)
    except Exception:
        pass
    await asyncio.sleep(0.6)

    detected = await _extract_editable_fields(page)
    return [
        QuestionField(
            question_text_snippet=f.nearby_text[:240],
            locator_css=f'[data-sf-field-id="{f.field_id}"]',
            tag=f.tag,
            role=f.role,
            input_type=f.input_type,
        )
        for f in detected
    ]


def match_questions_to_answers(
    question_snippets: list[str],
    answers: list[dict[str, str]],
) -> list[tuple[str, str, str, float]]:
    cleaned_answers = []
    for i, item in enumerate(answers):
        q = (item.get("question") or "").strip()
        a = _clean_answer(item.get("answer") or "")
        if not a:
            continue
        cleaned_answers.append((i, q, a))

    if not cleaned_answers:
        return [(q, q, "", 0.0) for q in question_snippets]

    matches: list[tuple[str, str, str, float]] = []
    used_indexes: set[int] = set()

    for idx, question in enumerate(question_snippets):
        best: tuple[int, str, str, float] | None = None
        for ans_idx, ans_question, ans_text in cleaned_answers:
            if ans_idx in used_indexes:
                continue
            score = _similarity(question, ans_question)
            if best is None or score > best[3]:
                best = (ans_idx, ans_question, ans_text, score)

        if best is None:
            fallback_idx = min(idx, len(cleaned_answers) - 1)
            _, ans_question, ans_text = cleaned_answers[fallback_idx]
            score = _similarity(question, ans_question)
            matches.append((question, ans_question, ans_text, score))
            continue

        used_indexes.add(best[0])
        matches.append((question, best[1], best[2], best[3]))

    return matches


async def fill_detected_fields(
    page: Page,
    fields: list[QuestionField],
    answers: list[dict[str, str]],
) -> int:
    """Legacy wrapper used by paster AP Classroom flow."""
    if not fields:
        return 0

    det_fields = [
        DetectedField(
            index=i,
            field_id=f.locator_css.split('"')[1] if '"' in f.locator_css else f"legacy-{i}",
            tag=f.tag,
            role=f.role,
            input_type=f.input_type,
            nearby_text=f.question_text_snippet,
        )
        for i, f in enumerate(fields)
    ]

    matches = match_answers_to_fields(det_fields, answers)
    filled = 0
    for idx, (fld, answer_text, score) in enumerate(matches):
        if not answer_text.strip():
            continue
        loc = page.locator(f'[data-sf-field-id="{fld.field_id}"]').first
        try:
            await click_with_retry(loc, timeout_ms=20_000)
            await _clear_field(page, loc)
            await _human_type(page, answer_text)
            filled += 1
            logger.info("  fill_detected_fields: filled %d/%d score=%.2f", idx + 1, len(fields), score)
        except Exception as exc:
            logger.warning("  fill_detected_fields: failed field %d: %s", idx + 1, exc)
        await asyncio.sleep(random.uniform(0.8, 2.0))
    return filled


async def fill_doc_sections(
    section_prompts: list[str],
    answers: list[dict[str, str]],
    place_answer_under_prompt: Callable[[str, str], Awaitable[bool]],
) -> int:
    if not section_prompts:
        return 0

    matches = match_questions_to_answers(section_prompts, answers)
    filled = 0
    for idx, (prompt, matched_question, answer_text, score) in enumerate(matches, start=1):
        if not answer_text.strip():
            continue
        ok = await place_answer_under_prompt(prompt, answer_text)
        if ok:
            filled += 1
            logger.info(
                "  fill_doc_sections: filled %d/%d | prompt='%s' | score=%.2f",
                idx, len(section_prompts), prompt[:60], score,
            )
            await asyncio.sleep(random.uniform(0.8, 2.0))
        else:
            logger.warning("  fill_doc_sections: could not place answer under prompt: %s", prompt[:60])
    return filled
