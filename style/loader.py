import logging
import random
from pathlib import Path

from config.settings import settings

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".txt", ".md", ".docx", ".pdf"}
NUM_EXAMPLES = 6


def _read_text_file(path: Path) -> str:
    if path.suffix == ".docx":
        try:
            import zipfile
            import xml.etree.ElementTree as ET

            with zipfile.ZipFile(path) as z:
                xml_content = z.read("word/document.xml")
            root = ET.fromstring(xml_content)
            ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
            paragraphs = root.findall(".//w:p", ns)
            return "\n".join(
                "".join(node.text or "" for node in p.findall(".//w:t", ns))
                for p in paragraphs
            )
        except Exception:
            return f"[DOCX file: {path.name}]"

    if path.suffix == ".pdf":
        try:
            with open(path, "rb") as f:
                content = f.read()
            text = content.decode("utf-8", errors="ignore")
            printable = "".join(c for c in text if c.isprintable() or c in "\n\t")
            return printable[:3000]
        except Exception:
            return f"[PDF file: {path.name}]"

    return path.read_text(encoding="utf-8", errors="ignore")


def load_style_examples(count: int = NUM_EXAMPLES) -> list[str]:
    past_dir = settings.past_work_dir
    if not past_dir.exists():
        logger.warning("Past work directory not found: %s", past_dir)
        return []

    files = [
        f
        for f in past_dir.iterdir()
        if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS
    ]

    if not files:
        logger.warning("No past work samples found in %s", past_dir)
        return []

    selected = random.sample(files, min(count, len(files)))
    examples = []

    for f in selected:
        text = _read_text_file(f)
        if text and len(text) > 50:
            trimmed = text[:2000]
            examples.append(trimmed)
            logger.info("Loaded style example: %s (%d chars)", f.name, len(trimmed))

    logger.info("Loaded %d style examples", len(examples))
    return examples
