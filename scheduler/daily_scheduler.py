import logging
import signal
import sys

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from config.settings import settings
from workflow.agent import run_workflow

logger = logging.getLogger(__name__)


def start_scheduler() -> None:
    scheduler = BlockingScheduler()

    hour_range = f"{settings.schedule_start_hour}-{settings.schedule_end_hour}"

    scheduler.add_job(
        run_workflow,
        trigger=CronTrigger(
            day_of_week=settings.schedule_days,
            hour=hour_range,
            minute=f"*/{settings.schedule_interval_minutes}",
        ),
        id="studyflow_scheduled",
        name="StudyFlow Scheduled Run",
        replace_existing=True,
        misfire_grace_time=600,
        coalesce=True,
        max_instances=1,
    )

    def shutdown(signum, frame):
        logger.info("Shutting down scheduler...")
        scheduler.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    logger.info(
        "Scheduler started: every %d min, %s %02d:00-%02d:00",
        settings.schedule_interval_minutes,
        settings.schedule_days,
        settings.schedule_start_hour,
        settings.schedule_end_hour,
    )

    scheduler.print_jobs()
    scheduler.start()
