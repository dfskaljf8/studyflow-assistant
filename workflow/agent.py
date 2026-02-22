import asyncio
import logging
import random
import time
from typing import TypedDict

from langgraph.graph import StateGraph, END

from classroom.scanner import Assignment, scan_all_assignments
from classroom.downloader import download_materials
from classroom.paster import paste_draft
from style.loader import load_style_examples
from drafting.llm_drafter import generate_draft
from google_services.docs_writer import create_draft_doc
from google_services.sheets_logger import log_assignment
from google_services.email_sender import send_daily_summary
from browser.session import close_browser
from config.settings import settings

logger = logging.getLogger(__name__)


class WorkflowState(TypedDict):
    assignments: list[Assignment]
    current_index: int
    style_examples: list[str]
    processed: list[dict]
    errors: list[str]


def _run_async(coro):
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(asyncio.run, coro).result()
    return asyncio.run(coro)


def scan_node(state: WorkflowState) -> dict:
    logger.info("=== Scanning assignments ===")
    assignments = _run_async(scan_all_assignments())
    return {"assignments": assignments, "current_index": 0}


def load_style_node(state: WorkflowState) -> dict:
    logger.info("=== Loading style examples ===")
    examples = load_style_examples()
    return {"style_examples": examples}


def process_assignment_node(state: WorkflowState) -> dict:
    idx = state["current_index"]
    assignments = state["assignments"]

    if idx >= len(assignments):
        return {}

    a = assignments[idx]
    logger.info("=== Processing [%d/%d]: %s ===", idx + 1, len(assignments), a.title)

    processed = list(state.get("processed", []))
    errors = list(state.get("errors", []))

    try:
        downloaded = _run_async(download_materials(a))
        material_texts = []
        for p in downloaded:
            if p.suffix in (".txt", ".md", ".csv"):
                material_texts.append(p.read_text(errors="ignore")[:2000])

        draft = generate_draft(a, state["style_examples"], material_texts)

        doc_link = _run_async(create_draft_doc(a.title, draft))

        pasted = _run_async(paste_draft(a, draft))

        _run_async(log_assignment(
            course_name=a.course_name,
            title=a.title,
            due_date_str=a.due_date_str or "No due date",
            draft_link=doc_link,
            status="Draft Pasted - Ready for Review" if pasted else "Draft Saved (paste failed)",
        ))

        processed.append({
            "course_name": a.course_name,
            "title": a.title,
            "due_date_str": a.due_date_str or "No due date",
            "draft_link": doc_link,
            "assignment_link": a.assignment_url,
        })

        logger.info("Completed: %s", a.title)

    except Exception as exc:
        msg = f"Error processing '{a.title}': {exc}"
        logger.exception(msg)
        errors.append(msg)

    return {
        "current_index": idx + 1,
        "processed": processed,
        "errors": errors,
    }


def should_continue(state: WorkflowState) -> str:
    if state["current_index"] < len(state["assignments"]):
        return "delay_and_process"
    return "send_summary"


def delay_node(state: WorkflowState) -> dict:
    if state["current_index"] > 0:
        delay = random.uniform(settings.delay_min_seconds, settings.delay_max_seconds)
        logger.info("Waiting %.0f seconds before next assignment...", delay)
        time.sleep(delay)
    return {}


def summary_node(state: WorkflowState) -> dict:
    logger.info("=== Sending daily summary ===")
    try:
        _run_async(asyncio.wait_for(
            send_daily_summary(state.get("processed", [])),
            timeout=settings.summary_email_timeout_seconds,
        ))
    except TimeoutError:
        logger.warning(
            "Summary email timed out after %ss; skipping",
            settings.summary_email_timeout_seconds,
        )
    except Exception:
        logger.exception("Summary email failed")

    try:
        _run_async(close_browser())
    except Exception:
        logger.exception("Failed to close browser cleanly")

    total = len(state["assignments"])
    done = len(state.get("processed", []))
    errs = len(state.get("errors", []))
    logger.info("Run complete: %d/%d processed, %d errors", done, total, errs)
    return {}


def build_workflow() -> StateGraph:
    graph = StateGraph(WorkflowState)

    graph.add_node("scan", scan_node)
    graph.add_node("load_style", load_style_node)
    graph.add_node("delay", delay_node)
    graph.add_node("process_assignment", process_assignment_node)
    graph.add_node("send_summary", summary_node)

    graph.set_entry_point("scan")
    graph.add_edge("scan", "load_style")
    graph.add_edge("load_style", "delay")
    graph.add_edge("delay", "process_assignment")

    graph.add_conditional_edges(
        "process_assignment",
        should_continue,
        {
            "delay_and_process": "delay",
            "send_summary": "send_summary",
        },
    )

    graph.add_edge("send_summary", END)
    return graph.compile()


def run_workflow() -> None:
    app = build_workflow()
    initial_state: WorkflowState = {
        "assignments": [],
        "current_index": 0,
        "style_examples": [],
        "processed": [],
        "errors": [],
    }
    app.invoke(initial_state)
