"""Application configuration for MonoPilot AI."""

from functools import lru_cache
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Runtime settings loaded from environment variables and .env file."""

    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.8-flash"
    gemini_fallback_models: str = "gemini-3.5-flash-lite,gemini-3.1-flash-lite"
    gemini_timeout_ms: int = 30000
    monobank_token: str = ""
    database_path: str = str(BASE_DIR / "monopilot.db")
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    max_agent_steps: int = 6

    @property
    def fallback_model_list(self) -> list[str]:
        """Return ordered fallback model names excluding the primary configured model."""
        models: list[str] = []
        for raw in self.gemini_fallback_models.split(","):
            name = raw.strip()
            if name and name != self.gemini_model and name not in models:
                models.append(name)
        return models


@lru_cache
def get_settings() -> Settings:
    """Return cached application settings instance."""
    return Settings()
