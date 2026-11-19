"""Configuration loaded from environment variables / .env file."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(PROJECT_ROOT / ".env")


TRUTHY = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    simplefin_access_url: str | None
    data_dir: Path
    timezone: str
    demo: bool = False

    @property
    def db_path(self) -> Path:
        return self.data_dir / "finance.db"

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.db_path.as_posix()}"

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"


    @property
    def categories_seed_file(self) -> Path:
        """Only used to create the initial categories of a new database."""
        return Path(os.getenv("FINANCE_CATEGORIES_SEED") or PROJECT_ROOT / "finance" / "default_categories.csv")

    @property
    def import_dir(self) -> Path:
        return self.data_dir / "import"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def log_file(self) -> Path:
        return self.log_dir / "finance.log"


def get_settings() -> Settings:
    demo = os.getenv("FINANCE_DEMO", "").strip().lower() in TRUTHY
    data_dir = Path(os.getenv("FINANCE_DATA_DIR") or PROJECT_ROOT / "data")
    if demo:
        # Demo data always lives in its own sub-folder so it can never touch real data.
        data_dir = data_dir / "demo"
    return Settings(
        simplefin_access_url=os.getenv("SIMPLEFIN_ACCESS_URL") or None,
        data_dir=data_dir,
        timezone=os.getenv("FINANCE_TIMEZONE", "America/New_York"),
        demo=demo,
    )
