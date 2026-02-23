import json
import logging
import re
import time
import urllib.request
import urllib.error

from config.settings import settings
from classroom.scanner import Assignment

logger = logging.getLogger(__name__)

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
MAX_RETRIES = 4
FALLBACK_MODELS = ["gemma-3-1b-it"]


def _clean_student_style_text(text: str) -> str:
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    cleaned = cleaned.replace("—", "-").replace("–", "-")
    cleaned = cleaned.replace("“", '"').replace("”", '"').replace("’", "'")
    cleaned = cleaned.replace(";", ",")
    cleaned = re.sub(r"([!?.,])\1+", r"\1", cleaned)
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _build_prompt(
    assignment: Assignment,
    style_examples: list[str],
    material_texts: list[str],
) -> str:
    examples_block = ""
    for i, ex in enumerate(style_examples, 1):
        examples_block += f"\n--- Example {i} of my past writing ---\n{ex}\n"

    materials_block = ""
    for i, mt in enumerate(material_texts, 1):
        materials_block += f"\n--- Attached Material {i} ---\n{mt}\n"

    return f"""You are writing a homework response in a student's personal voice.

CRITICAL RULES:
- Match the student's writing style and punctuation from the examples as closely as possible
- Keep punctuation simple and light (mostly periods/commas). Avoid semicolons, em dashes, and overly formal transitions
- Do NOT force slang that is not present in the examples
- NEVER sound robotic, formal, or AI-generated
- Keep wording natural, like real student writing with normal imperfections
- Answer the assignment accurately and completely
- Read both the Classroom instructions and all attached material text before writing
- If attached materials include template prompts/questions/boxes, answer in the same order and keep each answer concise
- Keep it the right length for this assignment (not too long, not too short)

STUDENT'S WRITING STYLE EXAMPLES:
{examples_block}

ASSIGNMENT INFO:
Class: {assignment.course_name}
Title: {assignment.title}
Description: {assignment.description}

{f"ATTACHED MATERIALS:{materials_block}" if materials_block else ""}

Write the complete assignment response now. Output ONLY the assignment text, nothing else."""


def generate_draft(
    assignment: Assignment,
    style_examples: list[str],
    material_texts: list[str] | None = None,
) -> str:
    prompt = _build_prompt(assignment, style_examples, material_texts or [])
    generated = _call_gemini(prompt)
    return _clean_student_style_text(generated)


def _candidate_models() -> list[str]:
    models = [settings.gemini_model, *FALLBACK_MODELS]
    unique: list[str] = []
    seen = set()
    for model in models:
        if model and model not in seen:
            unique.append(model)
            seen.add(model)
    return unique


def _call_model(prompt: str, model: str) -> str:
    url = GEMINI_URL.format(model=model, key=settings.gemini_api_key)

    payload = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.8,
            "maxOutputTokens": 4096,
        },
    }).encode()

    logger.info("Calling Gemini model: %s", model)

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read())

            candidates = data.get("candidates", [])
            if not candidates:
                raise RuntimeError(f"Gemini returned no candidates: {data}")

            text = candidates[0]["content"]["parts"][0]["text"]
            logger.info("Draft generated: %d chars", len(text))
            return text

        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            body_lower = body.lower()
            hard_quota = "limit: 0" in body_lower

            if e.code == 429 and attempt < MAX_RETRIES and not hard_quota:
                wait = 15 * attempt
                logger.warning(
                    "Rate limited on %s (429). Retrying in %ds (attempt %d/%d)",
                    model,
                    wait,
                    attempt,
                    MAX_RETRIES,
                )
                time.sleep(wait)
            else:
                raise RuntimeError(f"Gemini {model} HTTP {e.code}: {body[:500]}") from e

        except urllib.error.URLError as e:
            if attempt < MAX_RETRIES:
                wait = 5 * attempt
                logger.warning(
                    "Network error on %s: %s. Retrying in %ds (attempt %d/%d)",
                    model,
                    e,
                    wait,
                    attempt,
                    MAX_RETRIES,
                )
                time.sleep(wait)
                continue
            raise RuntimeError(f"Gemini {model} network error: {e}") from e

    raise RuntimeError(f"Gemini {model} failed after retries")


def _call_gemini(prompt: str) -> str:
    errors: list[str] = []

    for model in _candidate_models():
        try:
            return _call_model(prompt, model)
        except Exception as exc:
            errors.append(f"{model}: {exc}")
            logger.warning("Model %s failed, trying next fallback if available", model)

    raise RuntimeError("All Gemini models failed. " + " | ".join(errors))
