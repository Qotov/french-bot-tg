"""Application configuration via pydantic-settings.

Values come from the environment / .env file. A few of them (reminder time,
writing time, limits) can be overridden at runtime through the app_settings
table; see db/repo.py.
"""

import re

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        protected_namespaces=(),
    )

    bot_token: str
    gemini_api_key: str
    # The bot owner (auto-registered as admin). ALLOWED_USER_ID is the
    # pre-pilot name of the same setting and still works.
    admin_user_id: int = Field(
        validation_alias=AliasChoices("ADMIN_USER_ID", "ALLOWED_USER_ID", "admin_user_id")
    )
    max_users: int = 50
    daily_llm_actions: int = 150  # per-user daily cap on LLM-backed actions
    tz: str = "Europe/Paris"
    db_url: str = "sqlite+aiosqlite:///data/frbot.db"
    # One model for everything: Gemini 3.5 Flash-Lite. The two knobs are kept
    # so enrichment/drills and correction could be split again via .env.
    model_fast: str = "gemini-3.5-flash-lite"
    model_smart: str = "gemini-3.5-flash-lite"
    # Pronunciation. Audio output is billed per token but each distinct phrase
    # is synthesised once and cached on disk for the whole cohort.
    model_tts: str = "gemini-3.1-flash-tts-preview"
    tts_voice: str = "Kore"
    tts_enabled: bool = True
    daily_new_limit: int = 15
    session_max: int = 30
    reminder_time: str = "08:30"
    writing_time: str = "19:00"
    desired_retention: float = 0.9

    @field_validator("reminder_time", "writing_time")
    @classmethod
    def _valid_time(cls, v: str) -> str:
        if not TIME_RE.match(v):
            raise ValueError(f"expected HH:MM, got {v!r}")
        return v

    @field_validator("desired_retention")
    @classmethod
    def _valid_retention(cls, v: float) -> float:
        if not 0.5 <= v <= 0.995:
            raise ValueError("desired_retention must be between 0.5 and 0.995")
        return v
