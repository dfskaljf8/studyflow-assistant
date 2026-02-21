import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from playwright.async_api import Page

from browser.session import new_page, check_logged_in
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


async def _save_debug_screenshot(page: Page, name: str) -> None:
    try:
        path = settings.project_root / f"debug_{name}.png"
        await page.screenshot(path=str(path), full_page=True)
        logger.info("Debug screenshot saved: %s", path)
    except Exception:
        pass


async def _get_course_links(page: Page) -> list[dict]:
    await page.goto("https://classroom.google.com", wait_until="domcontentloaded", timeout=60000)
    await asyncio.sleep(4)

    await _save_debug_screenshot(page, "homepage")

    courses = await page.evaluate("""
        () => {
            const results = [];
            const seen = new Set();
            // Try all link patterns for course cards
            const allLinks = document.querySelectorAll('a[href*="/c/"]');
            for (const a of allLinks) {
                const href = a.href || '';
                const match = href.match(/\\/c\\/([^/\\?]+)/);
                if (!match || seen.has(match[1])) continue;
                seen.add(match[1]);

                // Walk up to find the course card container and get name
                let name = '';
                let el = a;
                // Look for text content that looks like a course name
                const textEls = a.querySelectorAll('div, span, h1, h2, h3');
                for (const t of textEls) {
                    const txt = t.textContent.trim();
                    if (txt.length > 2 && txt.length < 120 && !txt.includes('\\n')) {
                        name = txt;
                        break;
                    }
                }
                if (!name) name = a.textContent.trim().split('\\n')[0].substring(0, 80);

                results.push({
                    id: match[1],
                    name: name || 'Unknown Course',
                    url: 'https://classroom.google.com/c/' + match[1]
                });
            }
            return results;
        }
    """)

    logger.info("Found %d courses", len(courses))
    return courses


async def _scan_todo_page(page: Page) -> list[dict]:
    """Use the Classroom To-do page which shows all assigned/missing work."""
    await page.goto("https://classroom.google.com/u/0/a/not-turned-in/all", wait_until="domcontentloaded", timeout=60000)
    await asyncio.sleep(5)

    await _save_debug_screenshot(page, "todo_page")

    items = await page.evaluate("""
        () => {
            const results = [];
            // Grab all links that point to assignment pages
            const links = document.querySelectorAll('a[href*="/c/"][href*="/a/"]');
            const seen = new Set();
            for (const a of links) {
                const href = a.href || '';
                if (seen.has(href)) continue;
                seen.add(href);

                // Get title from the link or its children
                let title = '';
                const textNodes = a.querySelectorAll('div, span');
                for (const t of textNodes) {
                    const txt = t.textContent.trim();
                    if (txt.length > 2 && txt.length < 200) {
                        title = txt;
                        break;
                    }
                }
                if (!title) title = a.textContent.trim().split('\\n')[0];
                if (!title || title.length < 2) continue;

                // Try to find due date and class name near this link
                let dueText = '';
                let className = '';
                const parent = a.closest('li, div[class], tr') || a.parentElement;
                if (parent) {
                    const allText = parent.textContent || '';
                    const dueMatch = allText.match(/(Due|No due date|Missing|Assigned).*/i);
                    if (dueMatch) dueText = dueMatch[0].trim().substring(0, 60);
                }

                results.push({
                    title: title.substring(0, 200),
                    url: href.split('?')[0],
                    due_text: dueText,
                    class_name: className
                });
            }
            return results;
        }
    """)

    logger.info("To-do page found %d assignment links", len(items))
    return items


async def _scan_course_classwork(page: Page, course: dict) -> list[dict]:
    """Fallback: scan individual course's classwork tab."""
    url = f"https://classroom.google.com/c/{course['id']}/a/not-turned-in/all"
    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    await asyncio.sleep(4)

    await _save_debug_screenshot(page, f"course_{course['id']}")

    items = await page.evaluate("""
        () => {
            const results = [];
            const links = document.querySelectorAll('a[href*="/a/"]');
            const seen = new Set();
            for (const a of links) {
                const href = a.href || '';
                if (seen.has(href) || !href.includes('/a/')) continue;
                seen.add(href);

                let title = '';
                const textNodes = a.querySelectorAll('div, span');
                for (const t of textNodes) {
                    const txt = t.textContent.trim();
                    if (txt.length > 2 && txt.length < 200) {
                        title = txt;
                        break;
                    }
                }
                if (!title) title = a.textContent.trim().split('\\n')[0];
                if (!title || title.length < 2) continue;

                let dueText = '';
                const parent = a.closest('li, div[class], tr') || a.parentElement;
                if (parent) {
                    const allText = parent.textContent || '';
                    const dueMatch = allText.match(/(Due|No due date|Missing|Assigned).*/i);
                    if (dueMatch) dueText = dueMatch[0].trim().substring(0, 60);
                }

                results.push({
                    title: title.substring(0, 200),
                    url: href.split('?')[0],
                    due_text: dueText
                });
            }
            return results;
        }
    """)

    return items


async def _enrich_assignment(page: Page, assignment: Assignment) -> None:
    if not assignment.assignment_url:
        return

    try:
        await page.goto(assignment.assignment_url, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(3)

        info = await page.evaluate("""
            () => {
                // Grab all visible text blocks as potential description
                let desc = '';
                const candidates = document.querySelectorAll(
                    '[class*="z3vRcc"], [class*="tLDEHd"], [dir="ltr"], .description'
                );
                for (const el of candidates) {
                    const t = el.textContent.trim();
                    if (t.length > 10 && t.length > desc.length) desc = t;
                }

                const attachments = [];
                const links = document.querySelectorAll(
                    'a[href*="drive.google.com"], a[href*="docs.google.com"], a[href*="youtube.com"]'
                );
                for (const l of links) attachments.push(l.href);

                return {
                    description: desc.substring(0, 2000),
                    attachments: [...new Set(attachments)]
                };
            }
        """)

        assignment.description = info.get("description", "")
        assignment.attachment_urls = info.get("attachments", [])

    except Exception:
        logger.warning("Could not enrich: %s", assignment.title)


async def scan_all_assignments() -> list[Assignment]:
    page = await new_page()

    try:
        await check_logged_in(page)

        # First try the global to-do page
        todo_items = await _scan_todo_page(page)

        # If to-do page returned nothing, fall back to per-course scanning
        if not todo_items:
            logger.info("To-do page empty, falling back to per-course scan")
            courses = await _get_course_links(page)
            for course in courses:
                logger.info("Scanning course: %s", course["name"])
                try:
                    items = await _scan_course_classwork(page, course)
                    for item in items:
                        item["class_name"] = course["name"]
                    todo_items.extend(items)
                    logger.info("  Found %d items in %s", len(items), course["name"])
                except Exception:
                    logger.exception("Error scanning %s", course["name"])
                await asyncio.sleep(2)

        # Deduplicate by URL
        seen_urls = set()
        all_assignments: list[Assignment] = []
        for item in todo_items:
            url = item.get("url", "")
            if url in seen_urls:
                continue
            seen_urls.add(url)

            a = Assignment(
                course_name=item.get("class_name", ""),
                title=item["title"],
                due_date_str=item.get("due_text", ""),
                assignment_url=url,
            )
            all_assignments.append(a)

        # Enrich each assignment (get description + attachments)
        for a in all_assignments:
            logger.info("Enriching: %s", a.title)
            await _enrich_assignment(page, a)
            await asyncio.sleep(1.5)

            # If we didn't get a course name from to-do page, try from the assignment page
            if not a.course_name:
                try:
                    name = await page.evaluate("""
                        () => {
                            const el = document.querySelector('[class*="tLDEHd"], [class*="uDEFge"]');
                            return el ? el.textContent.trim() : '';
                        }
                    """)
                    if name:
                        a.course_name = name
                except Exception:
                    pass

        logger.info("Total pending assignments: %d", len(all_assignments))
        return all_assignments

    finally:
        await page.close()
