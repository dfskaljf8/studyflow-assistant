(async () => {
  const API_KEY = "%%GEMINI_KEY%%";
  const API_MODE = "%%API_MODE%%"; // "gemini" or "openrouter"

  // Google Gemini direct
  const GEMINI_URL = `https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key=${API_KEY}`;
  const GEMINI_FALLBACK = `https://generativelanguage.googleapis.com/v1beta/models/gemma-3-1b-it:generateContent?key=${API_KEY}`;

  // OpenRouter (OpenAI-compatible)
  const OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions";

  const IGNORE_COURSES = [
    "fbla", "deca", "speech & debate", "honor society", "nhs",
    "sat prep", "sat math", "math honor"
  ];
  const IGNORE_TYPES = ["leq", "dbq", "mcq", "saq", "frq"];
  const APUSH_RE = /ap\s*u\.?s\.?\s*hist|apush/i;

  const STYLE_EXAMPLES = [
    "Hope, because it keeps going and is the reason why I continue to do the stuff that I do. Honor because I was born with respect to my duty and my loved ones. Integrity, because I was raised to not cheat people.",
    "With many of my business endeavours, I often had to choose with being honest with my clients about the progress of the work, and sometimes I would have a lot of other stuff to focus on too, so sometimes progress gets delayed, but I would try to remain honest with them about it, and then quickly finish it."
  ];

  function shouldSkip(title, course) {
    const combined = `${title} ${course}`.toLowerCase();
    if (IGNORE_COURSES.some(c => combined.includes(c))) return true;
    if (APUSH_RE.test(combined)) return true;
    if (IGNORE_TYPES.some(t => new RegExp(`\\b${t}\\b`, "i").test(title))) return true;
    return false;
  }

  function log(msg) {
    console.log(`[StudyFlow] ${msg}`);
    if (!window._sfLog) window._sfLog = [];
    window._sfLog.push(msg);
  }

  // --- UI Panel ---
  function createPanel() {
    let panel = document.getElementById("sf-panel");
    if (panel) panel.remove();
    panel = document.createElement("div");
    panel.id = "sf-panel";
    panel.style.cssText = "position:fixed;top:10px;right:10px;width:420px;max-height:80vh;overflow-y:auto;background:#1a1a2e;color:#eee;font-family:monospace;font-size:13px;padding:16px;border-radius:12px;z-index:99999;box-shadow:0 4px 24px rgba(0,0,0,0.5);";
    panel.innerHTML = `<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
      <b style="font-size:15px;">StudyFlow Assistant</b>
      <span id="sf-close" style="cursor:pointer;font-size:18px;">&times;</span>
    </div><div id="sf-status" style="color:#0f0;">Starting...</div><div id="sf-results" style="margin-top:10px;"></div>`;
    document.body.appendChild(panel);
    document.getElementById("sf-close").onclick = () => panel.remove();
    return panel;
  }

  function setStatus(msg) {
    const el = document.getElementById("sf-status");
    if (el) el.textContent = msg;
    log(msg);
  }

  function addResult(html) {
    const el = document.getElementById("sf-results");
    if (el) el.innerHTML += html + "<br>";
  }

  // --- LLM API ---
  async function callLLM(prompt) {
    if (API_MODE === "openrouter") {
      return await callOpenRouter(prompt);
    }
    return await callGeminiDirect(prompt);
  }

  async function callGeminiDirect(prompt) {
    for (const url of [GEMINI_URL, GEMINI_FALLBACK]) {
      try {
        const resp = await fetch(url, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            contents: [{ parts: [{ text: prompt }] }],
            generationConfig: { temperature: 0.7, maxOutputTokens: 2048 }
          })
        });
        if (!resp.ok) continue;
        const data = await resp.json();
        const text = data?.candidates?.[0]?.content?.parts?.[0]?.text;
        if (text && text.trim()) return text.trim();
      } catch (e) {
        log(`Gemini error: ${e.message}`);
      }
    }
    return null;
  }

  async function callOpenRouter(prompt) {
    const models = ["google/gemini-2.0-flash-exp:free", "google/gemma-3-1b-it:free"];
    for (const model of models) {
      try {
        const resp = await fetch(OPENROUTER_URL, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "Authorization": `Bearer ${API_KEY}`,
            "HTTP-Referer": "https://classroom.google.com",
          },
          body: JSON.stringify({
            model: model,
            messages: [{ role: "user", content: prompt }],
            temperature: 0.7,
            max_tokens: 2048
          })
        });
        if (!resp.ok) continue;
        const data = await resp.json();
        const text = data?.choices?.[0]?.message?.content;
        if (text && text.trim()) return text.trim();
      } catch (e) {
        log(`OpenRouter error: ${e.message}`);
      }
    }
    return null;
  }

  // --- Scan To-Do Page ---
  async function scanAssignments() {
    setStatus("Navigating to To-do page...");
    const todoUrl = "https://classroom.google.com/u/0/a/not-turned-in/all";

    const resp = await fetch(todoUrl, { credentials: "include" });
    const html = await resp.text();

    const parser = new DOMParser();
    const doc = parser.parseFromString(html, "text/html");

    // Classroom is an SPA so fetch gives the shell. We need to scrape the live page.
    // Instead, navigate to the page and scrape the DOM directly.
    return null; // Will use live DOM approach
  }

  async function scanLiveTodo() {
    setStatus("Scanning To-do page...");

    // We need to be on the to-do page
    if (!window.location.href.includes("/a/not-turned-in") && !window.location.href.includes("/a/")) {
      window.location.href = "https://classroom.google.com/u/0/a/not-turned-in/all";
      return null; // Page will reload, bookmarklet needs to be clicked again
    }

    // Wait for content
    await new Promise(r => setTimeout(r, 3000));

    const assignments = [];
    // Find assignment items: look for links to /a/ detail pages
    const allLinks = document.querySelectorAll('a[href*="/c/"][href*="/a/"][href*="/details"]');
    const seen = new Set();

    for (const link of allLinks) {
      const href = link.getAttribute("href") || "";
      if (seen.has(href) || href.includes("not-turned-in")) continue;
      seen.add(href);

      // Walk up to find the assignment card
      let card = link.closest("[data-assignment-id]") || link.closest("li") || link.parentElement?.parentElement;
      let text = (card?.innerText || link.innerText || "").trim();

      // Extract title (first meaningful line)
      let lines = text.split("\n").map(l => l.trim()).filter(l => l.length > 3);
      let title = lines[0] || "Unknown";
      let course = "";

      // Try to find course name
      for (const line of lines) {
        if (IGNORE_COURSES.some(c => line.toLowerCase().includes(c)) || line.includes("Block") || line.includes("Period")) {
          course = line;
          break;
        }
      }

      const fullUrl = href.startsWith("http") ? href : `https://classroom.google.com${href}`;

      if (!shouldSkip(title, course)) {
        assignments.push({ title, course, url: fullUrl, text });
      }
    }

    // Also try the simpler approach: find all visible assignment cards
    if (assignments.length === 0) {
      const items = document.querySelectorAll('[class*="assignment"], [data-stream-item-id]');
      for (const item of items) {
        const text = item.innerText.trim();
        const link = item.querySelector('a[href*="/details"]');
        if (link) {
          const href = link.getAttribute("href") || "";
          const fullUrl = href.startsWith("http") ? href : `https://classroom.google.com${href}`;
          const lines = text.split("\n").filter(l => l.trim().length > 3);
          const title = lines[0] || "Unknown";
          if (!seen.has(href) && !shouldSkip(title, text)) {
            seen.add(href);
            assignments.push({ title, course: "", url: fullUrl, text });
          }
        }
      }
    }

    return assignments;
  }

  // --- Get Assignment Details ---
  async function getAssignmentDetails(url) {
    try {
      // Open in hidden iframe to get details
      const iframe = document.createElement("iframe");
      iframe.style.cssText = "position:fixed;left:-9999px;width:1px;height:1px;";
      document.body.appendChild(iframe);

      return new Promise((resolve) => {
        iframe.onload = async () => {
          await new Promise(r => setTimeout(r, 3000));
          try {
            const doc = iframe.contentDocument;
            const description = doc?.body?.innerText || "";

            // Find Google Doc links
            const docLinks = [];
            const links = doc?.querySelectorAll('a[href*="docs.google.com/document"]') || [];
            for (const l of links) {
              docLinks.push(l.getAttribute("href"));
            }

            iframe.remove();
            resolve({ description, docLinks });
          } catch (e) {
            iframe.remove();
            resolve({ description: "", docLinks: [] });
          }
        };
        iframe.src = url;
        setTimeout(() => { iframe.remove(); resolve({ description: "", docLinks: [] }); }, 10000);
      });
    } catch (e) {
      return { description: "", docLinks: [] };
    }
  }

  // --- Generate Draft ---
  async function generateDraft(assignment) {
    const prompt = `You are writing a homework response in a student's personal voice.

CRITICAL RULES:
- Match this student's writing style: casual, simple punctuation (periods/commas), no semicolons or em dashes
- NEVER sound robotic, formal, or AI-generated
- Keep wording natural with normal imperfections
- Answer accurately and completely
- If there are numbered questions, answer each one separately with its number
- Put each answer in its RESPECTIVE position
- Keep it the right length (not too long, not too short)

STUDENT STYLE EXAMPLES:
${STYLE_EXAMPLES.join("\n\n")}

ASSIGNMENT:
Title: ${assignment.title}
Course: ${assignment.course}
Instructions: ${assignment.text.substring(0, 1500)}

OUTPUT RULES:
- If there are numbered questions, format as "1. answer\\n2. answer" etc.
- Return plain text only, no markdown, no bold, no headers
- No placeholder text like <text> or [insert]

Write the response now. Output ONLY the answer text.`;

    return await callLLM(prompt);
  }

  // --- Paste into Classroom Question Textarea ---
  async function pasteIntoClassroom(url, draft) {
    // Navigate to the assignment page
    const win = window.open(url, "_blank");
    if (!win) {
      log("Popup blocked - allow popups for classroom.google.com");
      return false;
    }

    return new Promise((resolve) => {
      setTimeout(async () => {
        try {
          const textarea = win.document.querySelector('textarea[placeholder*="answer" i], textarea[placeholder*="type" i]');
          if (textarea) {
            textarea.focus();
            textarea.value = draft;
            textarea.dispatchEvent(new Event("input", { bubbles: true }));
            textarea.dispatchEvent(new Event("change", { bubbles: true }));
            log("Pasted into Classroom textarea");
            resolve(true);
          } else {
            log("No textarea found on assignment page");
            resolve(false);
          }
        } catch (e) {
          log(`Paste error: ${e.message}`);
          resolve(false);
        }
      }, 5000);
    });
  }

  // --- Main ---
  try {
    createPanel();

    if (!GEMINI_KEY || GEMINI_KEY === "%%GEMINI_KEY%%") {
      setStatus("ERROR: No API key. Re-create bookmarklet with your key.");
      return;
    }

    const assignments = await scanLiveTodo();
    if (assignments === null) {
      setStatus("Redirecting to To-do page... Click bookmarklet again after page loads.");
      return;
    }

    if (assignments.length === 0) {
      setStatus("No assignments found. Make sure you're on the To-do page.");
      addResult('<span style="color:#ff0;">Try clicking the "Assigned" or "Missing" tab first, then click the bookmarklet again.</span>');
      return;
    }

    setStatus(`Found ${assignments.length} assignment(s). Generating drafts...`);

    for (let i = 0; i < assignments.length; i++) {
      const a = assignments[i];
      setStatus(`[${i + 1}/${assignments.length}] Drafting: ${a.title}`);
      addResult(`<div style="border-bottom:1px solid #333;padding:8px 0;">
        <b style="color:#4fc3f7;">${i + 1}. ${a.title}</b><br>
        <span style="color:#888;">${a.course}</span>`);

      const draft = await generateDraft(a);

      if (draft) {
        addResult(`<div style="background:#0d1117;padding:8px;border-radius:6px;margin:4px 0;white-space:pre-wrap;font-size:12px;max-height:200px;overflow-y:auto;">${draft}</div>
          <div style="margin:4px 0;">
            <button onclick="navigator.clipboard.writeText(this.dataset.draft).then(()=>this.textContent='Copied!')" 
              data-draft="${draft.replace(/"/g, '&quot;')}" 
              style="background:#1976d2;color:#fff;border:none;padding:4px 12px;border-radius:4px;cursor:pointer;font-size:12px;">
              Copy Draft
            </button>
            <a href="${a.url}" target="_blank" style="color:#4fc3f7;font-size:12px;margin-left:8px;">Open Assignment</a>
          </div>
        </div>`);
        log(`Draft generated for: ${a.title} (${draft.length} chars)`);
      } else {
        addResult(`<span style="color:#f44;">Draft failed - API error</span></div>`);
      }

      // Small delay between API calls
      if (i < assignments.length - 1) {
        await new Promise(r => setTimeout(r, 2000));
      }
    }

    setStatus(`Done! ${assignments.length} assignment(s) processed.`);
    addResult('<hr style="border-color:#333;"><span style="color:#0f0;">Click "Copy Draft" then open the assignment to paste.</span>');

  } catch (err) {
    setStatus(`Error: ${err.message}`);
    log(`Fatal: ${err.stack}`);
  }
})();
