from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class CriteriaConfig(BaseModel):
    roles: list[str]
    domains: list[str]
    levels: list[str]


class LlmConfig(BaseModel):
    enabled: bool = True
    temperature: float = 0
    max_input_chars: int = 6000


class AppConfig(BaseModel):
    poll_interval_seconds: int = 60
    recent_messages_limit: int = 50
    dry_run: bool = False
    source_channels: list[str]
    destination_channel: str
    forward_with_user: bool = False
    criteria: CriteriaConfig
    llm: LlmConfig = Field(default_factory=LlmConfig)


class EnvSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    telegram_api_id: int
    telegram_api_hash: str
    telegram_bot_token: Optional[str] = None
    openai_api_key: Optional[str] = None
    openai_model: str = "gpt-4.1-mini"
    config_path: Path = Path("config.yaml")
    database_path: Path = Path("data/processed.sqlite3")
    telegram_session_name: str = "sessions/source_reader"
    bot_session_name: str = "sessions/destination_bot"


def load_config() -> tuple[EnvSettings, AppConfig]:
    load_dotenv()
    env = EnvSettings()
    with env.config_path.open("r", encoding="utf-8") as file:
        raw_config: dict[str, Any] = yaml.safe_load(file) or {}
    return env, AppConfig.model_validate(raw_config)
