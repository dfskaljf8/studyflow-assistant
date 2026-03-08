import logging
from pathlib import Path

from config.settings import settings

logger = logging.getLogger(__name__)


def _sanitize(name: str) -> str:
    return "".join(c if c.isalnum() or c in " _-." else "_" for c in name).strip()[:80]


async def create_draft_doc(title: str, body_text: str) -> str:
    """Save draft as a local text file. Returns the file path as the 'link'."""
    drafts_dir = settings.project_root / "drafts"
    drafts_dir.mkdir(exist_ok=True)

    filename = f"{_sanitize(title)} - Draft.txt"
    filepath = drafts_dir / filename

    filepath.write_text(
        f"DRAFT: {title}\n"
        f"{'=' * 60}\n\n"
        f"{body_text}\n",
        encoding="utf-8",
    )

    logger.info("Draft saved: %s", filepath)
    return str(filepath)
