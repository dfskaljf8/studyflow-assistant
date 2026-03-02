import asyncio
import concurrent.futures
import json
import logging
import os
import random
import threading
import time
from typing import TextIO, TypedDict, cast

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None

from langgraph.graph import StateGraph, END

from classroom.scanner import Assignment, scan_all_assignments
from classroom.downloader import download_materials
from classroom.paster import paste_draft
from style.loader import load_style_examples
from drafting.llm_drafter import generate_draft
from google_services.docs_writer import create_draft_doc
from google_services.sheets_logger import log_assignment
from google_services.email_sender import send_daily_summary
from browser.ap_session import close_ap_browser
from browser.session import close_browser
from config.settings import settings

logger = logging.getLogger(__name__)

SUCCESS_DELIVERY_METHODS = {
    "classroom_fields_filled",
    "doc_edited",
    "doc_copy_attached",
    "ap_classroom_fields_filled",
}

_async_loop: asyncio.AbstractEventLoop | None = None
_async_loop_thread: threading.Thread | None = None
_async_loop_ready = threading.Event()


class WorkflowState(TypedDict):
    assignments: list[Assignment]
    current_index: int
    style_examples: list[str]
    processed: list[dict]
    errors: list[str]
    assignment_state: dict


def _looks_like_url_list(text: str) -> bool:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return True
    url_lines = sum(
        1 for line in lines if line.startswith("http://") or line.startswith("https://")
    )
    return (url_lines / len(lines)) >= 0.8


def _strip_query(url: str) -> str:
    return (url or "").split("?", 1)[0].split("#", 1)[0]


def _assignment_key(assignment: Assignment) -> str:
    if assignment.class_id and assignment.assignment_id:
        return f"{assignment.class_id}:{assignment.assignment_id}"
    if assignment.assignment_id:
        return assignment.assignment_id
    if assignment.assignment_url:
        return _strip_query(assignment.assignment_url)
    return f"{assignment.course_name}|{assignment.title}".strip().lower()


def _load_assignment_state() -> dict:
    path = settings.assignment_state_file
    if not path.exists():
        return {"bootstrapped": False, "assignments": {}}

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("state root is not an object")
        assignments = payload.get("assignments", {})
        if not isinstance(assignments, dict):
            assignments = {}
        return {
            "bootstrapped": bool(payload.get("bootstrapped", False)),
            "assignments": assignments,
        }
    except Exception as exc:
        logger.warning("Failed to read assignment state file; resetting: %s", exc)
        return {"bootstrapped": False, "assignments": {}}


def _save_assignment_state(state: dict) -> None:
    path = settings.assignment_state_file
    safe_state = {
        "bootstrapped": bool(state.get("bootstrapped", False)),
        "assignments": state.get("assignments", {}),
    }
    path.write_text(json.dumps(safe_state, indent=2, sort_keys=True), encoding="utf-8")


def _monitor_should_process(assignment: Assignment, state: dict, now_ts: float) -> bool:
    key = _assignment_key(assignment)
    assignments = state.get("assignments", {})
    entry = assignments.get(key)
    if not isinstance(entry, dict):
        return True

    status = str(entry.get("status", "")).lower()
    if status == "success":
        return False

    if status == "bootstrapped_seen":
        return True

    if status == "failed":
        retry_after = max(1, settings.schedule_failed_retry_minutes) * 60
        last_attempt = float(entry.get("last_attempt_ts", 0) or 0)
        return (now_ts - last_attempt) >= retry_after

    return True


def _mark_assignment_state(
    state: dict,
    assignment: Assignment,
    *,
    status: str,
    delivery_method: str,
    delivery_details: str,
    pasted: bool,
) -> None:
    assignments = state.setdefault("assignments", {})
    key = _assignment_key(assignment)
    now_ts = time.time()
    entry = assignments.get(key)
    if not isinstance(entry, dict):
        entry = {}

    entry.update(
        {
            "course_name": assignment.course_name,
            "title": assignment.title,
            "assignment_url": _strip_query(assignment.assignment_url or ""),
            "status": status,
            "delivery_method": delivery_method,
            "delivery_details": delivery_details,
            "pasted": bool(pasted),
            "last_attempt_ts": now_ts,
            "last_seen_ts": now_ts,
        }
    )

    if status == "success":
        entry["last_success_ts"] = now_ts

    assignments[key] = entry
    _save_assignment_state(state)


def _acquire_run_lock() -> tuple[TextIO | None, str]:
    if fcntl is None:
        return None, ""

    lock_path = settings.project_root / ".studyflow_run.lock"
    lock_file = lock_path.open("a+", encoding="utf-8")

    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.seek(0)
        holder = lock_file.read().strip()
        lock_file.close()
        return None, holder

    lock_file.seek(0)
    lock_file.truncate()
    lock_file.write(str(os.getpid()))
    lock_file.flush()
    return lock_file, ""


def _release_run_lock(lock_file: TextIO | None) -> None:
    if not lock_file:
        return

    try:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    finally:
        try:
            lock_file.close()
        except Exception:
            pass


def _ensure_async_loop() -> asyncio.AbstractEventLoop:
    global _async_loop, _async_loop_thread

    if _async_loop and _async_loop.is_running():
        return _async_loop

    _async_loop_ready.clear()

    def _loop_worker() -> None:
        global _async_loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        _async_loop = loop
        _async_loop_ready.set()
        loop.run_forever()
        loop.close()

    _async_loop_thread = threading.Thread(target=_loop_worker, daemon=True)
    _async_loop_thread.start()
    _async_loop_ready.wait(timeout=5)

    if not _async_loop:
        raise RuntimeError("Failed to initialize async loop")

    return _async_loop


def _shutdown_async_loop() -> None:
    global _async_loop, _async_loop_thread

    loop = _async_loop
    thread = _async_loop_thread

    if loop and loop.is_running():
        loop.call_soon_threadsafe(loop.stop)

    if thread and thread.is_alive():
        thread.join(timeout=5)

    _async_loop = None
    _async_loop_thread = None


def _run_async(coro, timeout_seconds: float | None = None):
    loop = _ensure_async_loop()

    async def _runner():
        if timeout_seconds is None:
            return await coro
        return await asyncio.wait_for(coro, timeout=timeout_seconds)

    future = asyncio.run_coroutine_threadsafe(_runner(), loop)
    try:
        result_timeout = None if timeout_seconds is None else timeout_seconds + 5
        return future.result(timeout=result_timeout)
    except concurrent.futures.TimeoutError:
        future.cancel()
        raise TimeoutError


def scan_node(state: WorkflowState) -> dict:
    logger.info("=== Scanning assignments ===")
    assignments = _run_async(scan_all_assignments())
    mode = os.getenv("STUDYFLOW_MODE", "").lower()
    assignment_state = _load_assignment_state()

    if mode == "schedule":
        now_ts = time.time()
        records = assignment_state.setdefault("assignments", {})

        if not assignment_state.get("bootstrapped", False):
            if settings.schedule_bootstrap_existing_assignments:
                for assignment in assignments:
                    key = _assignment_key(assignment)
                    records[key] = {
                        "course_name": assignment.course_name,
                        "title": assignment.title,
                        "assignment_url": _strip_query(assignment.assignment_url or ""),
                        "status": "bootstrapped_seen",
                        "delivery_method": "bootstrap",
                        "delivery_details": "initial_scheduler_baseline",
                        "pasted": False,
                        "last_seen_ts": now_ts,
                        "last_attempt_ts": 0,
                    }

                assignment_state["bootstrapped"] = True
                _save_assignment_state(assignment_state)
                logger.info(
                    "Scheduler baseline established with %d existing assignment(s); waiting for new items",
                    len(assignments),
                )
                return {
                    "assignments": [],
                    "current_index": 0,
                    "assignment_state": assignment_state,
                }

            assignment_state["bootstrapped"] = True

        selected: list[Assignment] = []
        for assignment in assignments:
            key = _assignment_key(assignment)
            entry = records.get(key)

            if _monitor_should_process(assignment, assignment_state, now_ts):
                selected.append(assignment)

            if not isinstance(entry, dict):
                entry = {}

            entry.update(
                {
                    "course_name": assignment.course_name,
                    "title": assignment.title,
                    "assignment_url": _strip_query(assignment.assignment_url or ""),
                    "last_seen_ts": now_ts,
                }
            )
            records[key] = entry

        _save_assignment_state(assignment_state)
        logger.info(
            "Monitor scan: %d visible, %d new/retry candidate(s)",
            len(assignments),
            len(selected),
        )
        assignments = selected

    return {
        "assignments": assignments,
        "current_index": 0,
        "assignment_state": assignment_state,
    }


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
    assignment_state = dict(state.get("assignment_state") or _load_assignment_state())

    try:
        logger.info("  Step 1/5: Collecting attachment links + doc instructions")
        downloaded = []
        try:
            downloaded = _run_async(download_materials(a), timeout_seconds=75)
        except TimeoutError:
            logger.warning(
                "  Attachment collection timed out; continuing without materials"
            )
        except Exception as exc:
            logger.warning("  Attachment collection skipped: %s", exc)

        material_texts = []
        for p in downloaded:
            if p.suffix in (".txt", ".md", ".csv"):
                text = p.read_text(errors="ignore")
                if p.name == "links.txt" and _looks_like_url_list(text):
                    continue
                text = text.strip()
                if not text:
                    continue
                material_texts.append(f"[Source: {p.name}]\n{text[:3500]}")

        logger.info("  Step 2/5: Generating draft")
        draft = generate_draft(a, state["style_examples"], material_texts)

        logger.info("  Step 3/5: Saving local draft copy")
        doc_link = ""
        try:
            doc_link = _run_async(create_draft_doc(a.title, draft), timeout_seconds=20)
        except Exception:
            logger.exception("  Failed to save local draft copy")

        pasted = False
        delivery_method = "not_attempted"
        delivery_details = ""
        attempts = max(1, settings.paste_retry_attempts)
        attempt_timeout = max(15, settings.paste_attempt_timeout_seconds)

        for attempt in range(1, attempts + 1):
            logger.info(
                "  Step 4/5: Pasting into Classroom (attempt %d/%d)", attempt, attempts
            )
            try:
                pasted = _run_async(
                    paste_draft(
                        assignment=a,
                        draft_text=draft,
                        style_examples=state["style_examples"],
                        material_texts=material_texts,
                    ),
                    timeout_seconds=attempt_timeout,
                )
                delivery_method = a.delivery_method or (
                    "delivered" if pasted else "failed"
                )
                delivery_details = a.delivery_details or ""
            except TimeoutError:
                logger.warning(
                    "  Paste attempt %d timed out after %ds", attempt, attempt_timeout
                )
                pasted = False
                delivery_method = "failed"
                delivery_details = "timeout"
            except Exception:
                logger.exception("  Paste attempt %d failed", attempt)
                pasted = False
                delivery_method = "failed"
                delivery_details = "exception"

            if pasted:
                break

            if attempt < attempts:
                logger.info("  Retrying paste...")
                time.sleep(1)

        if not pasted:
            logger.warning(
                "  Paste failed after %d attempt(s); local draft was still saved",
                attempts,
            )

        status_map = {
            "classroom_fields_filled": "Draft filled in Classroom response fields",
            "doc_edited": "Draft added to attached Google Doc",
            "doc_copy_attached": "Draft added to copied Google Doc and attached",
            "ap_classroom_fields_filled": "Draft filled in AP Classroom response fields",
            "skipped_mismatch": "Skipped due to assignment mismatch",
        }
        status_text = status_map.get(delivery_method, "Draft Saved (delivery failed)")

        attempt_status = (
            "success"
            if (pasted or delivery_method in SUCCESS_DELIVERY_METHODS)
            else "failed"
        )
        _mark_assignment_state(
            assignment_state,
            a,
            status=attempt_status,
            delivery_method=delivery_method,
            delivery_details=delivery_details,
            pasted=pasted,
        )

        logger.info("  Step 5/5: Logging assignment")
        try:
            _run_async(
                log_assignment(
                    course_name=a.course_name,
                    title=a.title,
                    due_date_str=a.due_date_str or "No due date",
                    draft_link=doc_link,
                    status=status_text,
                ),
                timeout_seconds=45,
            )
        except TimeoutError:
            logger.warning("  Sheet logging timed out; continuing")
        except Exception:
            logger.exception("  Failed to log assignment")

        processed.append(
            {
                "course_name": a.course_name,
                "title": a.title,
                "due_date_str": a.due_date_str or "No due date",
                "draft_link": doc_link,
                "assignment_link": a.assignment_url,
                "pasted": pasted,
                "delivery_method": delivery_method,
                "delivery_details": delivery_details,
            }
        )

        logger.info("Completed: %s", a.title)

    except Exception as exc:
        msg = f"Error processing '{a.title}': {exc}"
        logger.exception(msg)
        errors.append(msg)
        _mark_assignment_state(
            assignment_state,
            a,
            status="failed",
            delivery_method="exception",
            delivery_details=str(exc),
            pasted=False,
        )

    return {
        "current_index": idx + 1,
        "processed": processed,
        "errors": errors,
        "assignment_state": assignment_state,
    }


def should_continue(state: WorkflowState) -> str:
    if state["current_index"] < len(state["assignments"]):
        return "delay_and_process"
    return "send_summary"


def delay_node(state: WorkflowState) -> dict:
    idx = state["current_index"]
    total = len(state["assignments"])

    if idx <= 0:
        return {}

    # Avoid an unnecessary long delay right before the final assignment.
    if total > 1 and idx >= total - 1:
        logger.info("Skipping delay before final assignment")
        return {}

    mode = os.getenv("STUDYFLOW_MODE", "").lower()
    if mode == "run":
        delay_min = 10.0
        delay_max = 30.0
    elif mode == "schedule":
        delay_min = 0.0
        delay_max = 0.0
    else:
        delay_min = max(0, settings.delay_min_seconds)
        delay_max = max(delay_min, settings.delay_max_seconds)
    delay = random.uniform(delay_min, delay_max)

    logger.info("Waiting %.0f seconds before next assignment...", delay)
    remaining = delay
    while remaining > 0:
        sleep_for = min(30.0, remaining)
        time.sleep(sleep_for)
        remaining -= sleep_for

    return {}


def summary_node(state: WorkflowState) -> dict:
    logger.info("=== Sending daily summary ===")
    mode = os.getenv("STUDYFLOW_MODE", "").lower()
    if settings.send_email_summary:
        try:
            _run_async(
                send_daily_summary(state.get("processed", [])),
                timeout_seconds=settings.summary_email_timeout_seconds,
            )
        except TimeoutError:
            logger.warning(
                "Summary email timed out after %ss; skipping",
                settings.summary_email_timeout_seconds,
            )
        except Exception as exc:
            logger.warning("Failed to send summary email: %s", exc)
    else:
        logger.info("Email summary disabled by configuration")

    if mode != "schedule":
        try:
            _run_async(close_ap_browser(), timeout_seconds=20)
        except Exception:
            pass
        try:
            _run_async(close_browser(), timeout_seconds=20)
        except TimeoutError:
            logger.warning("Browser close timed out; continuing")
        except Exception as exc:
            logger.warning("Failed to close browser cleanly: %s", exc)
    else:
        logger.info("Schedule mode: keeping browser session active")

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


def run_workflow() -> WorkflowState:
    lock_file, holder = _acquire_run_lock()
    if fcntl is not None and lock_file is None:
        holder_text = f" (pid {holder})" if holder else ""
        msg = f"Another StudyFlow run is already active{holder_text}; skipping this run"
        logger.warning(msg)
        return {
            "assignments": [],
            "current_index": 0,
            "style_examples": [],
            "processed": [],
            "errors": [msg],
            "assignment_state": _load_assignment_state(),
        }

    app = build_workflow()
    initial_state: WorkflowState = {
        "assignments": [],
        "current_index": 0,
        "style_examples": [],
        "processed": [],
        "errors": [],
        "assignment_state": _load_assignment_state(),
    }

    try:
        final_state = app.invoke(initial_state)
        if isinstance(final_state, dict):
            return cast(WorkflowState, final_state)
        return initial_state
    finally:
        if os.getenv("STUDYFLOW_MODE", "").lower() != "schedule":
            _shutdown_async_loop()
        _release_run_lock(lock_file)
