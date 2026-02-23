import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from playwright.async_api import Page

from browser.session import get_page, check_logged_in, safe_goto
from config.settings import settings

logger = logging.getLogger(__name__)

IGNORE_KEYWORDS: list[str] = []


def _extract_ids_from_url(url: str) -> tuple[str, str]:
    if not url:
        return "", ""
    match = re.search(r"/c/([^/]+)/a/([^/?#]+)", url)
    if not match:
        return "", ""
    return match.group(1), match.group(2)


def _load_ignore_list() -> list[str]:
    global IGNORE_KEYWORDS
    if not IGNORE_KEYWORDS:
        raw = settings.ignore_courses
        IGNORE_KEYWORDS = [k.strip().lower() for k in raw.split(",") if k.strip()]
    return IGNORE_KEYWORDS


def _should_skip(title: str, class_name: str) -> bool:
    keywords = _load_ignore_list()
    combined = f"{title} {class_name}".lower()
    return any(kw in combined for kw in keywords)


@dataclass
class Assignment:
    course_name: str
    title: str
    due_date_str: str = ""
    due_date: datetime | None = None
    assignment_url: str = ""
    class_id: str = ""
    assignment_id: str = ""
    description: str = ""
    attachment_urls: list[str] = field(default_factory=list)
    delivery_method: str = ""
    delivery_details: str = ""


async def _save_debug(page: Page, name: str) -> None:
    try:
        safe_name = "".join(c if c.isalnum() or c in " _-" else "_" for c in name)[:40]
        path = settings.project_root / f"debug_{safe_name}.png"
        await page.screenshot(path=str(path), full_page=True)
        logger.info("Debug screenshot: %s", path)
    except Exception:
        pass


async def _scan_missing_tab(page: Page) -> list[dict]:
    """Scan only the Missing tab — these are the assignments you actually need to do."""
    # Go to to-do page
    await safe_goto(page, "https://classroom.google.com/u/0/a/not-turned-in/all",
                    wait_selector='a[href*="/c/"]')
    await asyncio.sleep(3)

    # Click "Missing" tab
    try:
        missing_tab = page.locator('a:has-text("Missing"), [role="tab"]:has-text("Missing")').first
        if await missing_tab.is_visible(timeout=5000):
            await missing_tab.click()
            await asyncio.sleep(3)
            logger.info("Clicked Missing tab")
    except Exception:
        logger.warning("Could not click Missing tab")

    # Expand all collapsed sections
    for label in ["This week", "Last week", "Earlier", "Next week", "Later"]:
        try:
            section = page.locator(f'text="{label}"').first
            if await section.is_visible(timeout=1500):
                await section.click()
                await asyncio.sleep(1.5)
        except Exception:
            pass

    await _save_debug(page, "missing_tab")

    items = await page.evaluate("""
        () => {
            const results = [];
            const seen = new Set();
            const allLinks = document.querySelectorAll('a[href*="/c/"][href*="/a/"]');

            for (const a of allLinks) {
                const href = (a.href || '').split('?')[0];
                if (seen.has(href)) continue;
                seen.add(href);

                const container = a.closest('li') || a.closest('[role="listitem"]') || a;
                const fullText = container.textContent || '';

                // Title: first meaningful text in the link
                let title = '';
                for (const s of a.querySelectorAll('div, span, p')) {
                    const t = s.textContent.trim();
                    if (t.length > 3 && t.length < 200 &&
                        !t.match(/^Posted/) && !t.match(/^Due/) &&
                        !t.match(/^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)/i) &&
                        !t.match(/^\\d{1,2}:\\d{2}/) &&
                        !t.match(/^(This|Last|Next)\\s+(week|month)/i) &&
                        !t.match(/^Earlier/i) &&
                        !t.match(/^(January|February|March|April|May|June|July|August|September|October|November|December)/i) &&
                        !t.match(/^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)/i)) {
                        title = t;
                        break;
                    }
                }
                if (!title) title = a.textContent.trim().split('\\n')[0].substring(0, 150);
                if (!title || title.length < 2) continue;

                // Class name: typically the second line under the title
                let className = '';
                let foundTitle = false;
                for (const s of container.querySelectorAll('div, span')) {
                    const t = s.textContent.trim();
                    if (t === title) { foundTitle = true; continue; }
                    if (foundTitle && t.length > 2 && t.length < 100 &&
                        !t.match(/^Posted/) && !t.match(/^Due/) &&
                        !t.match(/^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)/i)) {
                        className = t;
                        break;
                    }
                }

                // Due text
                let dueText = '';
                const dateMatch = fullText.match(/(Due|Missing|Posted).{0,60}/i);
                if (dateMatch) dueText = dateMatch[0].trim();

                results.push({
                    title: title,
                    url: href,
                    due_text: dueText,
                    class_name: className
                });
            }
            return results;
        }
    """)

    logger.info("Missing tab: found %d raw items", len(items))
    return items


async def _enrich_assignment(page: Page, assignment: Assignment) -> None:
    """Navigate to assignment detail page for description + attachments."""
    if not assignment.assignment_url:
        return

    try:
        await safe_goto(page, assignment.assignment_url, wait_selector='[role="main"]')
        await asyncio.sleep(2)

        # Check if page rendered - if blank, reload once
        body_len = await page.evaluate("() => document.body?.innerText?.length || 0")
        if body_len < 100:
            logger.info("  Page looks blank (%d chars), reloading...", body_len)
            try:
                await page.reload(wait_until="domcontentloaded", timeout=30000)
            except Exception:
                pass
            await asyncio.sleep(5)

        await _save_debug(page, f"assign_{assignment.title}")

        info = await page.evaluate("""
            () => {
                // Assignment title
                let assignmentTitle = '';
                for (const sel of ['h1', '[role="main"] h1', 'div[role="heading"][aria-level="1"]']) {
                    const el = document.querySelector(sel);
                    if (!el) continue;
                    const t = (el.textContent || '').trim();
                    if (t.length > 1 && t.length < 250) {
                        assignmentTitle = t;
                        break;
                    }
                }

                // Description
                let desc = '';
                for (const sel of ['[class*="z3vRcc"]', '[dir="ltr"]', '[role="main"] div']) {
                    for (const el of document.querySelectorAll(sel)) {
                        const t = el.textContent.trim();
                        if (t.length > 20 && t.length > desc.length && t.length < 5000) desc = t;
                    }
                    if (desc.length > 20) break;
                }

                // Course name from breadcrumb at top of page
                let courseName = '';
                // The breadcrumb shows "Classroom > CourseName" at the top
                const breadcrumb = document.querySelector('header a[href*="/c/"]');
                if (breadcrumb) {
                    courseName = breadcrumb.textContent.trim();
                }
                if (!courseName) {
                    for (const b of document.querySelectorAll('[class*="onkcGd"], [class*="uDEFge"]')) {
                        const t = b.textContent.trim();
                        if (t.length > 2 && t.length < 100) { courseName = t; break; }
                    }
                }

                // Attachments
                const attachments = [];
                for (const l of document.querySelectorAll(
                    'a[href*="drive.google.com"], a[href*="docs.google.com"], ' +
                    'a[href*="youtube.com"], a[href*="forms.google.com"]'
                )) attachments.push(l.href);

                return {
                    title: assignmentTitle,
                    description: desc.substring(0, 2000),
                    attachments: [...new Set(attachments)],
                    course_name: courseName
                };
            }
        """)

        canonical_title = info.get("title", "")
        if canonical_title:
            assignment.title = canonical_title

        assignment.description = info.get("description", "")
        assignment.attachment_urls = info.get("attachments", [])
        enriched_name = info.get("course_name", "")
        if enriched_name:
            assignment.course_name = enriched_name

    except Exception:
        logger.warning("Could not enrich: %s", assignment.title)


async def scan_all_assignments() -> list[Assignment]:
    page = await get_page()

    try:
        await check_logged_in(page)

        todo_items = await _scan_missing_tab(page)

        # Deduplicate + first-pass filter
        seen_urls = set()
        candidates: list[Assignment] = []
        skipped = 0

        for item in todo_items:
            url = item.get("url", "")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)

            title = item["title"]
            class_name = item.get("class_name", "")
            class_id, assignment_id = _extract_ids_from_url(url)

            if _should_skip(title, class_name):
                logger.info("  SKIP (pre-filter): %s — %s", class_name, title)
                skipped += 1
                continue

            candidates.append(Assignment(
                course_name=class_name,
                title=title,
                due_date_str=item.get("due_text", ""),
                assignment_url=url,
                class_id=class_id,
                assignment_id=assignment_id,
            ))

        logger.info("Pre-filter: %d candidates, %d skipped", len(candidates), skipped)

        # Enrich each and do a second-pass filter using the real course name
        final: list[Assignment] = []
        for a in candidates:
            logger.info("  Enriching: %s (%s)", a.title, a.course_name)
            await _enrich_assignment(page, a)
            await asyncio.sleep(2)

            # Second-pass filter with enriched course name
            if _should_skip(a.title, a.course_name):
                logger.info("  SKIP (post-enrich): %s — %s", a.course_name, a.title)
                continue

            final.append(a)

        logger.info("Total assignments to process: %d", len(final))
        return final

    except Exception:
        logger.exception("Scanner failed")
        return []
