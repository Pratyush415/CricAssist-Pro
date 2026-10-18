"""Settings loaded from environment variables and the local .env file."""

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MODELS = {
    "gemini": "gemini-3.8-flash",
    "claude": "claude-opus-5-5",
}


@dataclass(frozen=True)
class Settings:
    db_path: Path
    context_path: Path  # ChromaDB vector database of context passages
    cache_dir: Path  # fetched ESPNcricinfo pages
    llm_provider: str  # "gemini" or "claude"
    llm_model: str
    gemini_api_key: str
    anthropic_api_key: str


def load_dotenv(path: Path = Path(".env")) -> None:
    """Read KEY=value lines from .env into the environment.

    Variables already set in the environment win, so a shell export can
    override the file. Blank lines and # comments are skipped.
    """
    if not path.is_file():
        return
    for line in path.read_text("utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def load_settings() -> Settings:
    load_dotenv()
    provider = os.getenv("CRICASSIST_LLM_PROVIDER", "gemini").strip().lower()
    return Settings(
        db_path=Path(os.getenv("CRICASSIST_DB_PATH", "data/processed/cricassist.db")),
        context_path=Path(os.getenv("CRICASSIST_CONTEXT_PATH", "data/processed/context")),
        cache_dir=Path(os.getenv("CRICASSIST_CACHE_DIR", "data/raw/cricinfo_cache")),
        llm_provider=provider,
        llm_model=os.getenv("CRICASSIST_LLM_MODEL", "") or DEFAULT_MODELS.get(provider, ""),
        gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
    )
