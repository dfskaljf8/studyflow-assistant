import json
import logging
import re
import time
import urllib.request
import urllib.error
from typing import Any

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
    cleaned = re.sub(r"\*\*(.+?)\*\*", r"\1", cleaned)
    cleaned = re.sub(r"__(.+?)__", r"\1", cleaned)
    cleaned = re.sub(r"`([^`]+)`", r"\1", cleaned)
    cleaned = re.sub(r"^\s*#{1,6}\s*", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"<\s*/?\s*text\s*>", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"<\s*/?\s*answer\s*>", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"<[^>]{1,50}>", "", cleaned)
    cleaned = re.sub(r"^\s*[-*]\s+", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"([!?.,])\1+", r"\1", cleaned)
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _materials_look_like_template(material_texts: list[str]) -> bool:
    if not material_texts:
        return False
    combined = "\n".join(material_texts)[:15000]
    return bool(re.search(r"_{3,}|\[\s*\]|\(\s*\)|\b(question|prompt|response|answer)\b", combined, flags=re.IGNORECASE))


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

    template_mode = _materials_look_like_template(material_texts)
    if template_mode:
        output_rules = (
            "OUTPUT FORMAT RULES:\n"
            "- Keep answers aligned to template order\n"
            "- Return exactly one block per box/question\n"
            "- Use this exact format:\n"
            "[Answer 1]\\nactual answer for first box\\n\\n[Answer 2]\\nactual answer for second box\n"
            "- Do not include markdown, bold markers, headings, or bullet lists\n"
            "- Do not use placeholder text like <text> or [insert]\n"
        )
    else:
        output_rules = (
            "OUTPUT FORMAT RULES:\n"
            "- Return plain assignment text only\n"
            "- No markdown, no bold markers, no heading symbols\n"
            "- No placeholder text like <text> or [insert]\n"
        )

    return f"""You are writing a homework response in a student's personal voice.

CRITICAL RULES:
- Match the student's writing style and punctuation from the examples as closely as possible
- Keep punctuation simple and light (mostly periods/commas). Avoid semicolons, em dashes, and overly formal transitions
- Do NOT force slang that is not present in the examples
- NEVER sound robotic, formal, or AI-generated
- Keep wording natural, like real student writing with normal imperfections
- Answer the assignment accurately and completely
- Read both the Classroom instructions and all attached material text before writing
- If attached materials include template prompts/questions/boxes, answer EACH question separately in order
- NEVER combine multiple answers into one block. Each question/box gets its own separate answer.
- Keep it the right length for this assignment (not too long, not too short)
- Prefer simple punctuation and shorter sentences over polished formal phrasing
- Carefully read all on-screen instructions in the template before answering

STUDENT'S WRITING STYLE EXAMPLES:
{examples_block}

ASSIGNMENT INFO:
Class: {assignment.course_name}
Title: {assignment.title}
Description: {assignment.description}

{f"ATTACHED MATERIALS:{materials_block}" if materials_block else ""}

{output_rules}

Write the complete assignment response now. Output ONLY the assignment text, nothing else."""


def _build_structured_answers_prompt(
    assignment: Assignment,
    style_examples: list[str],
    material_texts: list[str],
    question_snippets: list[str],
    attachment_summary: str,
) -> str:
    examples_block = ""
    for i, ex in enumerate(style_examples, 1):
        examples_block += f"\n--- Example {i} of my past writing ---\n{ex}\n"

    materials_block = ""
    for i, mt in enumerate(material_texts, 1):
        materials_block += f"\n--- Attached Material {i} ---\n{mt}\n"

    questions_block = "\n".join(f"{i}. {q}" for i, q in enumerate(question_snippets, start=1))

    return f"""You are writing homework answers in a student's own writing style.

RESPONSE RULES (MANDATORY):
- Return ONLY valid JSON. No markdown, no code fences, no extra keys.
- JSON schema:
  {{
    "answers": [
      {{"index": 0, "question_snippet": "short match of question 1", "answer": "full answer in student style"}},
      {{"index": 1, "question_snippet": "short match of question 2", "answer": "..."}}
    ]
  }}
- "index" MUST match the question number (0-based) from the DETECTED QUESTIONS list.
- Always produce EXACTLY ONE answer object per detected question in the same order as provided.
- NEVER combine multiple answers into one. Each question gets its own separate answer object.
- Each answer will be typed into its own separate text box on the document. Do not cram everything together.
- If matching is uncertain, still return one answer per question using the closest snippet or positional order.
- Keep punctuation simple and natural. Avoid formal AI tone.
- No placeholders like <text>, [insert], or TODO.
- Read any on-screen instructions or prompts carefully and follow them exactly.

STUDENT STYLE EXAMPLES:
{examples_block}

ASSIGNMENT INFO:
Class: {assignment.course_name}
Title: {assignment.title}
Description: {assignment.description}

ATTACHMENT SUMMARY:
{attachment_summary}

{f"ATTACHED MATERIALS:{materials_block}" if materials_block else ""}

DETECTED QUESTIONS (IN ORDER):
{questions_block}

Now return the JSON object only."""


def _extract_json_payload(text: str) -> Any | None:
    raw = (text or "").strip()
    if not raw:
        return None

    cleaned = re.sub(r"^\s*```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```\s*$", "", cleaned).strip()

    for candidate in (cleaned, raw):
        try:
            return json.loads(candidate)
        except Exception:
            pass

    obj_match = re.search(r"\{[\s\S]*\}", cleaned)
    if obj_match:
        try:
            return json.loads(obj_match.group(0))
        except Exception:
            pass

    arr_match = re.search(r"\[[\s\S]*\]", cleaned)
    if arr_match:
        try:
            return json.loads(arr_match.group(0))
        except Exception:
            pass

    return None


def _split_fallback_answers(text: str, count: int) -> list[str]:
    cleaned = _clean_student_style_text(text)
    if not cleaned:
        return []

    blocks = [chunk.strip() for chunk in re.split(r"\n\s*\n+", cleaned) if chunk.strip()]
    if not blocks:
        blocks = [cleaned]

    if len(blocks) >= count:
        return blocks[:count]

    if count <= 1:
        return [cleaned]

    lines = [ln.strip() for ln in cleaned.splitlines() if ln.strip()]
    if len(lines) >= count:
        size = max(1, len(lines) // count)
        chunks: list[str] = []
        for i in range(0, len(lines), size):
            chunks.append("\n".join(lines[i:i + size]).strip())
            if len(chunks) >= count:
                break
        if chunks:
            return chunks

    return [cleaned]


def _parse_structured_answers(raw: str, question_snippets: list[str]) -> list[dict[str, str]]:
    payload = _extract_json_payload(raw)
    if payload is None:
        return []

    if isinstance(payload, dict):
        answers_raw = payload.get("answers", [])
    elif isinstance(payload, list):
        answers_raw = payload
    else:
        return []

    parsed: list[dict[str, str]] = []
    for idx, item in enumerate(answers_raw):
        if isinstance(item, dict):
            question = str(item.get("question") or item.get("question_snippet") or "").strip()
            answer = str(item.get("answer") or item.get("response") or "").strip()
            raw_index = item.get("index")
        elif isinstance(item, str):
            question = ""
            answer = item.strip()
            raw_index = None
        else:
            continue

        if not answer:
            continue

        answer = _clean_student_style_text(answer)
        if not question and idx < len(question_snippets):
            question = question_snippets[idx]

        entry: dict[str, str] = {"question": question, "answer": answer}
        if raw_index is not None:
            entry["index"] = str(raw_index)
            entry["question_snippet"] = question

        parsed.append(entry)

    return parsed


def generate_structured_answers(
    assignment: Assignment,
    style_examples: list[str],
    material_texts: list[str],
    question_snippets: list[str],
    attachment_summary: str,
) -> list[dict[str, str]]:
    if not question_snippets:
        return []

    prompt = _build_structured_answers_prompt(
        assignment=assignment,
        style_examples=style_examples,
        material_texts=material_texts,
        question_snippets=question_snippets,
        attachment_summary=attachment_summary,
    )

    raw = _call_gemini(prompt)
    parsed = _parse_structured_answers(raw, question_snippets)
    if parsed:
        return parsed

    repair_prompt = (
        prompt
        + "\n\nYour previous output was not valid JSON. Return ONLY valid JSON now using the exact schema."
    )
    repaired = _call_gemini(repair_prompt)
    parsed = _parse_structured_answers(repaired, question_snippets)
    if parsed:
        return parsed

    fallback_chunks = _split_fallback_answers(raw or repaired, len(question_snippets))
    if not fallback_chunks:
        fallback_chunks = ["I completed this response in my normal writing style."]

    filled: list[dict[str, str]] = []
    for idx, question in enumerate(question_snippets):
        chunk = fallback_chunks[min(idx, len(fallback_chunks) - 1)]
        filled.append({"question": question, "answer": chunk})

    return filled


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
                raw_body = resp.read()

            if not raw_body or not raw_body.strip():
                if attempt < MAX_RETRIES:
                    wait = 10 * attempt
                    logger.warning("Model %s returned empty body (possible rate limit). Retrying in %ds", model, wait)
                    time.sleep(wait)
                    continue
                raise RuntimeError(f"Gemini {model} returned empty response body after retries")

            data = json.loads(raw_body)

            candidates = data.get("candidates", [])
            if not candidates:
                raise RuntimeError(f"Gemini returned no candidates: {data}")

            text = (candidates[0].get("content", {}).get("parts", [{}])[0].get("text") or "").strip()
            if not text:
                if attempt < MAX_RETRIES:
                    wait = 10 * attempt
                    logger.warning("Model %s returned empty text in candidates. Retrying in %ds", model, wait)
                    time.sleep(wait)
                    continue
                raise RuntimeError(f"Gemini {model} returned empty text after retries")

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
