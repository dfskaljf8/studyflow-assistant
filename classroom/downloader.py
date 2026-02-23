import logging
from pathlib import Path

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

    unique_urls = list(dict.fromkeys(assignment.attachment_urls))
    links_file = local_dir / "links.txt"
    links_file.write_text("\n".join(unique_urls) + "\n", encoding="utf-8")

    logger.info("Saved %d attachment link(s): %s", len(unique_urls), links_file)
    return [links_file]
