import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from playwright.async_api import Page

from browser.session import get_page, check_logged_in, safe_goto
from config.settings import settings

logger = logging.getLogger(__name__)


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
    # Navigate to the to-do page
    await safe_goto(page, "https://classroom.google.com/u/0/a/not-turned-in/all",
                    wait_selector='a[href*="/c/"]')
    await asyncio.sleep(3)

    # Also click "Missing" tab to capture those too
    try:
        missing_tab = page.locator('a:has-text("Missing"), [role="tab"]:has-text("Missing")').first
        if await missing_tab.is_visible(timeout=3000):
            await missing_tab.click()
            await asyncio.sleep(3)
    except Exception:
        pass

    # Click "View all" if present to expand the list
    try:
        view_all = page.locator('a:has-text("View all"), button:has-text("View all")').first
        if await view_all.is_visible(timeout=2000):
            await view_all.click()
            await asyncio.sleep(2)
    except Exception:
        pass

    # Also expand "Next week" and "Later" sections
    for section_text in ["Next week", "Later", "This week"]:
        try:
            section = page.locator(f'text="{section_text}"').first
            if await section.is_visible(timeout=1000):
                await section.click()
                await asyncio.sleep(1.5)
        except Exception:
            pass

    await _save_debug(page, "todo_expanded")

    items = await page.evaluate("""
        () => {
            const results = [];
            const seen = new Set();

            // Get all links that go to assignment pages
            const allLinks = document.querySelectorAll('a[href*="/c/"][href*="/a/"]');
            for (const a of allLinks) {
                const href = (a.href || '').split('?')[0];
                if (seen.has(href)) continue;
                seen.add(href);

                // Get the full text content of the link's container
                const container = a.closest('li') || a.closest('[role="listitem"]') || a;
                const fullText = container.textContent || '';

                // Extract title: first meaningful text line
                let title = '';
                const spans = a.querySelectorAll('div, span, p');
                for (const s of spans) {
                    const t = s.textContent.trim();
                    // Skip class names and dates (they contain specific patterns)
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

                // Extract class name (usually the second line under the title)
                let className = '';
                const allSpans = container.querySelectorAll('div, span');
                let foundTitle = false;
                for (const s of allSpans) {
                    const t = s.textContent.trim();
                    if (t === title) { foundTitle = true; continue; }
                    if (foundTitle && t.length > 2 && t.length < 100 &&
                        !t.match(/^Posted/) && !t.match(/^Due/)) {
                        className = t;
                        break;
                    }
                }

                // Extract due/posted info
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

    logger.info("To-do page: found %d items", len(items))
    return items


async def _enrich_assignment(page: Page, assignment: Assignment) -> None:
    """Navigate to assignment page to get description and attachments."""
    if not assignment.assignment_url:
        return

    try:
        await safe_goto(page, assignment.assignment_url, wait_selector='[dir="ltr"], [role="main"]')
        await asyncio.sleep(2)

        info = await page.evaluate("""
            () => {
                // Try multiple selectors for description
                let desc = '';
                const selectors = [
                    '[class*="z3vRcc"]',
                    '[dir="ltr"]',
                    '[class*="tLDEHd"]',
                    '[role="main"] div'
                ];
                for (const sel of selectors) {
                    const els = document.querySelectorAll(sel);
                    for (const el of els) {
                        const t = el.textContent.trim();
                        if (t.length > 20 && t.length > desc.length && t.length < 5000) {
                            desc = t;
                        }
                    }
                    if (desc.length > 20) break;
                }

                // Get class name from breadcrumb or header
                let courseName = '';
                const breadcrumbs = document.querySelectorAll(
                    'a[href*="/c/"] span, [class*="onkcGd"], [class*="uDEFge"]'
                );
                for (const b of breadcrumbs) {
                    const t = b.textContent.trim();
                    if (t.length > 2 && t.length < 100) {
                        courseName = t;
                        break;
                    }
                }

                const attachments = [];
                const links = document.querySelectorAll(
                    'a[href*="drive.google.com"], a[href*="docs.google.com"], a[href*="youtube.com"]'
                );
                for (const l of links) attachments.push(l.href);

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

        # Deduplicate by URL
        seen_urls = set()
        all_assignments: list[Assignment] = []
        for item in todo_items:
            url = item.get("url", "")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)

            a = Assignment(
                course_name=item.get("class_name", ""),
                title=item["title"],
                due_date_str=item.get("due_text", ""),
                assignment_url=url,
            )
            all_assignments.append(a)

        logger.info("Found %d unique assignments, enriching each...", len(all_assignments))

        for a in all_assignments:
            logger.info("  Enriching: %s", a.title)
            await _enrich_assignment(page, a)
            await asyncio.sleep(2)

        logger.info("Total pending assignments: %d", len(all_assignments))
        return all_assignments

    except Exception:
        logger.exception("Scanner failed")
        return []
