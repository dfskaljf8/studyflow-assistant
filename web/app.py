import json
import os
import urllib.request
import urllib.error
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)

GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
MODELS = ["gemini-2.0-flash", "gemma-3-1b-it"]

STYLE_EXAMPLES = [
    "Hope, because it keeps going and is the reason why I continue to do the stuff that I do. Honor because I was born with respect to my duty and my loved ones. Integrity, because I was raised to not cheat people. Fairness because I believe in helping everyone and keeping them to the same standard. Honesty, goes with honor, it is part of your duty to be honest with your loved ones.",
    "With many of my business endeavours, I often had to choose with being honest with my clients about the progress of the work, and sometimes I would have a lot of other stuff to focus on too, so sometimes progress gets delayed, but I would try to remain honest with them about it, and then quickly finish it.",
]


def call_gemini(prompt):
    for model in MODELS:
        url = GEMINI_URL.format(model=model, key=GEMINI_KEY)
        payload = json.dumps({
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.7, "maxOutputTokens": 2048},
        }).encode()
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
            text = data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
            if text.strip():
                return text.strip()
        except Exception:
            continue
    return None


def build_prompt(title, course, instructions):
    style_block = "\n\n".join(STYLE_EXAMPLES)
    return f"""You are writing a homework response in a student's personal voice.

CRITICAL RULES:
- Match this student's writing style: casual, simple punctuation (periods/commas), no semicolons or em dashes
- NEVER sound robotic, formal, or AI-generated
- Keep wording natural, like real student writing with normal imperfections
- Answer accurately and completely
- If there are numbered questions, answer each one separately with its number
- Put each answer in its RESPECTIVE position -- never combine answers
- Keep it the right length for this assignment (not too long, not too short)

STUDENT'S WRITING STYLE EXAMPLES:
{style_block}

ASSIGNMENT:
Title: {title}
Course: {course}
Instructions: {instructions[:3000]}

OUTPUT RULES:
- If there are numbered questions, format as "1. answer\\n2. answer" etc.
- Return plain text only, no markdown, no bold, no headers
- No placeholder text like <text> or [insert]

Write the complete assignment response now. Output ONLY the answer text."""


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/generate", methods=["POST"])
def generate():
    data = request.get_json()
    title = data.get("title", "").strip()
    course = data.get("course", "").strip()
    instructions = data.get("instructions", "").strip()

    if not instructions:
        return jsonify({"error": "No instructions provided"}), 400
    if not GEMINI_KEY:
        return jsonify({"error": "No API key configured on server"}), 500

    prompt = build_prompt(title, course, instructions)
    draft = call_gemini(prompt)
    if draft:
        return jsonify({"draft": draft})
    return jsonify({"error": "LLM failed to generate draft"}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
