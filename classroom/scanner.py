import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from playwright.async_api import Page

from browser.session import get_page, check_logged_in, safe_goto
from config.settings import settings

logger = logging.getLogger(__name__)

IGNORE_KEYWORDS: list[str] = []


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
    description: str = ""
    attachment_urls: list[str] = field(default_factory=list)


async def _save_debug(page: Page, name: str) -> None:
    try:
        path = settings.project_root / f"debug_{name}.png"
        await page.screenshot(path=str(path), full_page=True)
        logger.info("Debug screenshot: %s", path)
    except Exception:
        pass


async def _scan_todo_page(page: Page) -> list[dict]:
    """Scrape the To-do page for all assigned + missing work."""
    await safe_goto(page, "https://classroom.google.com/u/0/a/not-turned-in/all",
                    wait_selector='a[href*="/c/"]')
    await asyncio.sleep(3)

    # Click "Missing" tab to capture incomplete work
    try:
        missing_tab = page.locator('a:has-text("Missing"), [role="tab"]:has-text("Missing")').first
        if await missing_tab.is_visible(timeout=3000):
            await missing_tab.click()
            await asyncio.sleep(3)
    except Exception:
        pass

    # Expand collapsed sections
    for label in ["View all", "Next week", "Later", "This week"]:
        try:
            el = page.locator(f'text="{label}"').first
            if await el.is_visible(timeout=1500):
                await el.click()
                await asyncio.sleep(1.5)
        except Exception:
            pass

    await _save_debug(page, "todo_expanded")

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

                // Title: first meaningful text
                let title = '';
                for (const s of a.querySelectorAll('div, span, p')) {
                    const t = s.textContent.trim();
                    if (t.length > 3 && t.length < 200 &&
                        !t.match(/^Posted/) && !t.match(/^Due/) &&
                        !t.match(/^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)/) &&
                        !t.match(/^\\d{1,2}:\\d{2}/)) {
                        title = t;
                        break;
                    }
                }
                if (!title) title = a.textContent.trim().split('\\n')[0].substring(0, 150);
                if (!title || title.length < 2) continue;

                // Class name: line after the title
                let className = '';
                let foundTitle = false;
                for (const s of container.querySelectorAll('div, span')) {
                    const t = s.textContent.trim();
                    if (t === title) { foundTitle = true; continue; }
                    if (foundTitle && t.length > 2 && t.length < 100 &&
                        !t.match(/^Posted/) && !t.match(/^Due/)) {
                        className = t;
                        break;
                    }
                }

                let dueText = '';
                const dateMatch = fullText.match(/(Due|Posted|Missing).{0,60}/i);
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

    logger.info("To-do page: found %d raw items", len(items))
    return items


async def _enrich_assignment(page: Page, assignment: Assignment) -> None:
    """Navigate to assignment detail page for description + attachments."""
    if not assignment.assignment_url:
        return

    try:
        await safe_goto(page, assignment.assignment_url, wait_selector='[role="main"]')

        # If page looks blank, try reload once
        body_text = await page.evaluate("() => document.body?.innerText?.length || 0")
        if body_text < 100:
            logger.info("  Page looks blank, reloading...")
            await page.reload(wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(4)

        await _save_debug(page, f"assign_{assignment.title[:20].replace(' ','_')}")

        info = await page.evaluate("""
            () => {
                let desc = '';
                for (const sel of ['[class*="z3vRcc"]', '[dir="ltr"]', '[role="main"] div']) {
                    for (const el of document.querySelectorAll(sel)) {
                        const t = el.textContent.trim();
                        if (t.length > 20 && t.length > desc.length && t.length < 5000) desc = t;
                    }
                    if (desc.length > 20) break;
                }

                let courseName = '';
                for (const b of document.querySelectorAll('a[href*="/c/"] span, [class*="onkcGd"]')) {
                    const t = b.textContent.trim();
                    if (t.length > 2 && t.length < 100) { courseName = t; break; }
                }

                const attachments = [];
                for (const l of document.querySelectorAll(
                    'a[href*="drive.google.com"], a[href*="docs.google.com"], a[href*="youtube.com"]'
                )) attachments.push(l.href);

                return {
                    description: desc.substring(0, 2000),
                    attachments: [...new Set(attachments)],
                    course_name: courseName
                };
            }
        """)

        assignment.description = info.get("description", "")
        assignment.attachment_urls = info.get("attachments", [])
        if not assignment.course_name and info.get("course_name"):
            assignment.course_name = info["course_name"]

    except Exception:
        logger.warning("Could not enrich: %s", assignment.title)


async def scan_all_assignments() -> list[Assignment]:
    page = await get_page()

    try:
        await check_logged_in(page)
        todo_items = await _scan_todo_page(page)

        # Deduplicate + filter
        seen_urls = set()
        all_assignments: list[Assignment] = []
        skipped = 0

        for item in todo_items:
            url = item.get("url", "")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)

            title = item["title"]
            class_name = item.get("class_name", "")

            if _should_skip(title, class_name):
                logger.info("  Skipping (club/extracurricular): %s — %s", class_name, title)
                skipped += 1
                continue

            all_assignments.append(Assignment(
                course_name=class_name,
                title=title,
                due_date_str=item.get("due_text", ""),
                assignment_url=url,
            ))

        if skipped:
            logger.info("Skipped %d items from ignored courses", skipped)

        logger.info("Found %d assignments to process, enriching...", len(all_assignments))

        for a in all_assignments:
            logger.info("  Enriching: %s", a.title)
            await _enrich_assignment(page, a)
            await asyncio.sleep(2)

        logger.info("Total pending assignments: %d", len(all_assignments))
        return all_assignments

    except Exception:
        logger.exception("Scanner failed")
        return []
