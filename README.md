# StudyFlow Assistant

Personal academic productivity tool that scans Google Classroom, generates style-matched drafts, and organizes everything for quick review.

**No Google Cloud Console or Apps Script needed.** Uses Playwright browser automation to work directly with your school Google account — bypasses all third-party OAuth blocks.

## Setup

### 1. Install Dependencies

```bash
cd ~/Projects/studyflow_assistant
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

### 2. Configure

```bash
cp .env.example .env
```

Edit `.env`:
- Set your LLM API key (Anthropic or xAI)
- Paste your StudyFlow Logs Google Sheet URL

### 3. Create a Google Sheet

In your school Google Drive, create a blank spreadsheet named "StudyFlow Logs" and paste its URL into `.env`.

### 4. Add Past Work Samples

Place 4-6 of your own past assignments (`.txt`, `.md`, `.docx`, or `.pdf`) in `my_past_work/`.

### 5. First-Time Login

```bash
python main.py login
```

A browser opens — sign into your school Google account. Your session is saved so you only do this once.

### 6. Run

```bash
# Process all pending assignments now
python main.py run

# Auto-run daily at 3pm
python main.py schedule
```

## How It Works

1. Opens a stealth Chromium browser logged into your school account
2. Scrapes Google Classroom for all pending/missing/upcoming assignments
3. Downloads attachments via the browser
4. Generates a style-matched draft for each using Claude or Grok
5. Pastes each draft into the Classroom assignment page
6. Creates a Google Doc copy of each draft
7. Logs everything to your Google Sheet
8. Sends you an email summary via Gmail

Human-like delays (3-12 min) between assignments.

## Modes

| Command | What it does |
|---|---|
| `python main.py login` | Sign into school account (first time only) |
| `python main.py run` | Process all assignments once |
| `python main.py schedule` | Run daily at 3pm automatically |
