"""Settings loaded from environment variables."""

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    anthropic_api_key: str
    db_path: Path


def load_settings() -> Settings:
    return Settings(
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        db_path=Path(os.getenv("CRICASSIST_DB_PATH", "data/processed/cricassist.db")),
    )
