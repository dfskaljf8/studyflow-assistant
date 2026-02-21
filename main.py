#!/usr/bin/env python3
import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config.settings import settings


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
        logger.info("Starting single run...")
        from workflow.agent import run_workflow
        run_workflow()
        logger.info("Single run complete.")

    elif args.mode == "schedule":
        logger.info("Starting scheduler mode...")
        from scheduler.daily_scheduler import start_scheduler
        start_scheduler()


if __name__ == "__main__":
    main()
