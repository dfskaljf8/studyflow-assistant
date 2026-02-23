#!/usr/bin/env python3
import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config.settings import settings


def _print_local_delivery_summary(final_state: dict[str, Any] | None) -> None:
    state = final_state or {}
    processed = state.get("processed") or []
    errors = state.get("errors") or []

    print("\n=== StudyFlow Local Delivery Summary ===")
    if not processed:
        print("No assignments were processed in this run.")

    for i, item in enumerate(processed, start=1):
        title = item.get("title", "(unknown title)")
        draft_link = item.get("draft_link") or "(draft path unavailable)"
        assignment_link = item.get("assignment_link") or "(no assignment link)"
        paste_status = "pasted" if item.get("pasted") else "not pasted"
        delivery_method = item.get("delivery_method") or "unknown"

        print(f"{i}. {title}")
        print(f"   Draft: {draft_link}")
        print(f"   Assignment: {assignment_link}")
        print(f"   Paste: {paste_status}")
        print(f"   Delivery method: {delivery_method}")

    if errors:
        print("\nErrors:")
        for err in errors:
            print(f"- {err}")
    print("========================================\n")


def setup_logging():
    log_file = settings.project_root / "studyflow.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file),
        ],
    )


def main():
    parser = argparse.ArgumentParser(description="StudyFlow Assistant")
    parser.add_argument(
        "mode",
        choices=["run", "schedule", "login"],
        help=(
            "'login' = open browser to sign into school account, "
            "'run' = execute once now, "
            "'schedule' = start daily scheduler"
        ),
    )
    args = parser.parse_args()

    setup_logging()
    logger = logging.getLogger("studyflow")

    settings.downloads_dir.mkdir(exist_ok=True)
    settings.past_work_dir.mkdir(exist_ok=True)

    if args.mode == "login":
        os.environ["STUDYFLOW_MODE"] = "login"
        import asyncio
        from browser.session import new_page, check_logged_in, close_browser

        async def do_login():
            page = await new_page()
            success = await check_logged_in(page)
            await page.close()
            await close_browser()
            if success:
                print("\nSession saved. You can now run: python main.py run")
            else:
                print("\nLogin failed. Please try again: python main.py login")

        asyncio.run(do_login())

    elif args.mode == "run":
        os.environ["STUDYFLOW_MODE"] = "run"
        logger.info("Starting single run...")
        from workflow.agent import run_workflow
        final_state = run_workflow()
        _print_local_delivery_summary(final_state)
        logger.info("Single run complete.")

    elif args.mode == "schedule":
        os.environ["STUDYFLOW_MODE"] = "schedule"
        logger.info("Starting scheduler mode...")
        from scheduler.daily_scheduler import start_scheduler
        start_scheduler()


if __name__ == "__main__":
    main()
