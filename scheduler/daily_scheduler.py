import logging
import signal
import sys
from datetime import datetime

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger

from config.settings import settings
from workflow.agent import run_workflow

logger = logging.getLogger(__name__)

WEEKDAY_MAP = {
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}


def _parse_allowed_weekdays() -> set[int]:
    tokens = [t.strip().lower()[:3] for t in (settings.schedule_days or "").split(",") if t.strip()]
    days = {WEEKDAY_MAP[t] for t in tokens if t in WEEKDAY_MAP}
    return days or {0, 1, 2, 3, 4}


def _in_active_window(now: datetime) -> bool:
    allowed_days = _parse_allowed_weekdays()
    if now.weekday() not in allowed_days:
        return False

    start_minutes = (settings.schedule_start_hour * 60) + settings.schedule_start_minute
    end_minutes = (settings.schedule_end_hour * 60) + settings.schedule_end_minute
    current_minutes = (now.hour * 60) + now.minute
    return start_minutes <= current_minutes <= end_minutes


def _poll_for_new_assignments() -> None:
    now = datetime.now()
    if not _in_active_window(now):
        return

    logger.info("Monitor tick at %s", now.strftime("%H:%M:%S"))
    try:
        run_workflow()
    except Exception:
        logger.exception("Scheduled monitor tick failed")


def start_scheduler() -> None:
    scheduler = BlockingScheduler()
    poll_seconds = max(15, settings.schedule_poll_seconds)

    scheduler.add_job(
        _poll_for_new_assignments,
        trigger=IntervalTrigger(seconds=poll_seconds),
        id="studyflow_scheduled",
        name="StudyFlow Continuous Monitor",
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
        "Scheduler started: poll every %ds, %s %02d:%02d-%02d:%02d",
        poll_seconds,
        settings.schedule_days,
        settings.schedule_start_hour,
        settings.schedule_start_minute,
        settings.schedule_end_hour,
        settings.schedule_end_minute,
    )

    _poll_for_new_assignments()

    scheduler.print_jobs()
    scheduler.start()
