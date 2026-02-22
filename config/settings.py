from pathlib import Path
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"

    studyflow_sheet_url: str = ""

    schedule_start_hour: int = 8
    schedule_end_hour: int = 14
    schedule_interval_minutes: int = 30
    schedule_days: str = "mon,tue,wed,thu,fri"

    delay_min_seconds: int = 180
    delay_max_seconds: int = 720
    summary_email_timeout_seconds: int = 180

    # Comma-separated keywords — courses matching any of these are skipped
    ignore_courses: str = (
        "FBLA,DECA,Speech and Debate,Speech & Debate,Honor Society,"
        "NHS,SAT Prep,SAT Math Boot Camp,Applicants,Math Honor"
    )

    @property
    def project_root(self) -> Path:
        return Path(__file__).resolve().parent.parent

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
