import asyncio
import logging
from pathlib import Path

from playwright.async_api import Page

from browser.session import new_page
from config.settings import settings
from classroom.scanner import Assignment

logger = logging.getLogger(__name__)


def _sanitize(name: str) -> str:
    return "".join(c if c.isalnum() or c in " _-." else "_" for c in name).strip()


async def download_materials(assignment: Assignment) -> list[Path]:
    if not assignment.attachment_urls:
        return []

    local_dir = settings.downloads_dir / _sanitize(assignment.course_name) / _sanitize(assignment.title)
    local_dir.mkdir(parents=True, exist_ok=True)

    page = await new_page()
    downloaded: list[Path] = []

    try:
        for url in assignment.attachment_urls:
            try:
                if "docs.google.com/document" in url:
                    doc_id = url.split("/d/")[1].split("/")[0]
                    export_url = f"https://docs.google.com/document/d/{doc_id}/export?format=txt"

                    async with page.expect_download(timeout=15000) as dl_info:
                        await page.goto(export_url)
                    download = await dl_info.value
                    dest = local_dir / (_sanitize(download.suggested_filename or "doc.txt"))
                    await download.save_as(str(dest))
                    downloaded.append(dest)
                    logger.info("Downloaded doc: %s", dest.name)

                elif "drive.google.com" in url:
                    file_id = None
                    if "/d/" in url:
                        file_id = url.split("/d/")[1].split("/")[0]
                    elif "id=" in url:
                        file_id = url.split("id=")[1].split("&")[0]

                    if file_id:
                        dl_url = f"https://drive.google.com/uc?export=download&id={file_id}"
                        async with page.expect_download(timeout=15000) as dl_info:
                            await page.goto(dl_url)
                        download = await dl_info.value
                        dest = local_dir / (_sanitize(download.suggested_filename or "file"))
                        await download.save_as(str(dest))
                        downloaded.append(dest)
                        logger.info("Downloaded drive file: %s", dest.name)

                else:
                    links_file = local_dir / "links.txt"
                    with links_file.open("a") as f:
                        f.write(url + "\n")
                    if links_file not in downloaded:
                        downloaded.append(links_file)

                await asyncio.sleep(1)

            except Exception:
                logger.warning("Could not download: %s", url)

    finally:
        await page.close()

    return downloaded
