import logging
import re
from html import unescape
from pathlib import Path

from playwright.async_api import Page

from browser.session import get_page
from config.settings import settings
from classroom.scanner import Assignment

logger = logging.getLogger(__name__)

MAX_DOC_ATTACHMENTS_TO_READ = 4
MAX_EXTRACTED_TEXT_CHARS = 12000
DOC_REQUEST_TIMEOUT_MS = 20000


def _sanitize(name: str) -> str:
    return "".join(c if c.isalnum() or c in " _-." else "_" for c in name).strip()


def _strip_query(url: str) -> str:
    return url.split("?", 1)[0].split("#", 1)[0]


def _extract_doc_id(url: str) -> str:
    match = re.search(r"/document/d/([^/?#]+)", url or "")
    return match.group(1) if match else ""


def _is_google_doc_url(url: str) -> bool:
    return "docs.google.com/document/d/" in (url or "")


def _clean_text(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    normalized = re.sub(r"[ \t]+", " ", normalized)
    normalized = normalized.strip()
    return normalized[:MAX_EXTRACTED_TEXT_CHARS]


def _html_to_text(html: str) -> str:
    body = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.IGNORECASE | re.DOTALL)
    body = re.sub(r"<style[^>]*>.*?</style>", " ", body, flags=re.IGNORECASE | re.DOTALL)
    body = re.sub(r"<br\s*/?>", "\n", body, flags=re.IGNORECASE)
    body = re.sub(r"</(p|div|li|tr|h[1-6])>", "\n", body, flags=re.IGNORECASE)
    body = re.sub(r"<[^>]+>", " ", body)
    return _clean_text(unescape(body))


async def _request_text(page: Page, url: str) -> str:
    try:
        response = await page.context.request.get(url, timeout=DOC_REQUEST_TIMEOUT_MS)
    except Exception:
        return ""

    try:
        if not response.ok:
            return ""
        return await response.text()
    except Exception:
        return ""
    finally:
        try:
            await response.dispose()
        except Exception:
            pass


async def _extract_doc_text(page: Page, doc_url: str) -> str:
    doc_id = _extract_doc_id(doc_url)
    if not doc_id:
        return ""

    txt_export_url = f"https://docs.google.com/document/d/{doc_id}/export?format=txt"
    txt_export = _clean_text(await _request_text(page, txt_export_url))
    if len(txt_export) >= 80:
        return txt_export

    mobilebasic_url = f"https://docs.google.com/document/d/{doc_id}/mobilebasic"
    mobilebasic_html = await _request_text(page, mobilebasic_url)
    mobilebasic_text = _html_to_text(mobilebasic_html) if mobilebasic_html else ""
    if len(mobilebasic_text) >= 80:
        return mobilebasic_text

    try:
        await page.goto(doc_url, wait_until="domcontentloaded", timeout=DOC_REQUEST_TIMEOUT_MS)
        text = await page.evaluate("() => document.body?.innerText || ''")
    except Exception:
        return ""

    return _clean_text(text)


async def download_materials(assignment: Assignment) -> list[Path]:
    if not assignment.attachment_urls:
        return []

    local_dir = settings.downloads_dir / _sanitize(assignment.course_name) / _sanitize(assignment.title)
    local_dir.mkdir(parents=True, exist_ok=True)

    unique_urls = list(dict.fromkeys(assignment.attachment_urls))
    links_file = local_dir / "links.txt"
    links_file.write_text("\n".join(unique_urls) + "\n", encoding="utf-8")

    saved_files: list[Path] = [links_file]
    logger.info("Saved %d attachment link(s): %s", len(unique_urls), links_file)

    doc_urls = [_strip_query(u) for u in unique_urls if _is_google_doc_url(_strip_query(u))]
    if not doc_urls:
        return saved_files

    page = await get_page()
    for index, doc_url in enumerate(doc_urls[:MAX_DOC_ATTACHMENTS_TO_READ], start=1):
        text = await _extract_doc_text(page, doc_url)
        if len(text) < 80:
            logger.info("Could not extract readable text from doc attachment: %s", doc_url)
            continue

        doc_id = _extract_doc_id(doc_url) or f"{index}"
        doc_file = local_dir / f"doc_context_{index}_{doc_id[:12]}.txt"
        doc_file.write_text(
            f"Source URL: {doc_url}\n\n{text}\n",
            encoding="utf-8",
        )
        saved_files.append(doc_file)
        logger.info("Extracted doc context: %s", doc_file)

    return saved_files
