from pathlib import Path
from pydantic_settings import BaseSettings


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ENV_FILE = _PROJECT_ROOT / ".env"


class Settings(BaseSettings):
    model_config = {"env_file": str(_ENV_FILE), "env_file_encoding": "utf-8"}

    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"

    studyflow_sheet_url: str = ""

    schedule_start_hour: int = 8
    schedule_end_hour: int = 14
    schedule_interval_minutes: int = 30
    schedule_days: str = "mon,tue,wed,thu,fri"

    delay_min_seconds: int = 180
    delay_max_seconds: int = 720
    summary_email_timeout_seconds: int = 90

    send_email_summary: bool = False
    paste_retry_attempts: int = 2
    paste_attempt_timeout_seconds: int = 55

    # Comma-separated keywords — courses matching any of these are skipped
    ignore_courses: str = (
        "FBLA,DECA,Speech and Debate,Speech & Debate,Honor Society,"
        "NHS,SAT Prep,SAT Math Boot Camp,Applicants,Math Honor"
    )

    @property
    def project_root(self) -> Path:
        return _PROJECT_ROOT

    @property
    def downloads_dir(self) -> Path:
        return self.project_root / "downloads"

    @property
    def past_work_dir(self) -> Path:
        return self.project_root / "my_past_work"

    @property
    def browser_data_dir(self) -> Path:
        return self.project_root / ".browser_data"


settings = Settings()
