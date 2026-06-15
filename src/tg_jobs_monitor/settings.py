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
    require_domains: bool = True
    require_levels: bool = True


class LlmConfig(BaseModel):
    enabled: bool = True
    temperature: float = 0
    max_input_chars: int = 6000


class HhSearchConfig(BaseModel):
    source: str
    text: str
    area: Optional[int] = None


class HhConfig(BaseModel):
    enabled: bool = False
    per_page: int = 20
    pages: int = 1
    request_delay_seconds: float = 0.0
    min_salary_rur: int = 0
    require_remote: bool = False
    allowed_domain_keywords: list[str] = Field(default_factory=list)
    excluded_grade_terms: list[str] = Field(default_factory=list)
    excluded_company_keywords: list[str] = Field(default_factory=list)
    searches: list[HhSearchConfig] = Field(default_factory=list)


class TonJobsConfig(BaseModel):
    enabled: bool = False
    jobs_url: str = "https://jobs.ton.org/jobs"
    request_delay_seconds: float = 0.0
    max_description_chars: int = 8000
    min_poll_interval_hours: float = 24.0


class AppConfig(BaseModel):
    poll_interval_seconds: int = 60
    recent_messages_limit: int = 50
    request_delay_seconds: float = 0
    request_delay_jitter_seconds: float = 0
    publish_on_first_run: bool = False
    dry_run: bool = False
    source_channels: list[str]
    destination_channel: str
    criteria: CriteriaConfig
    llm: LlmConfig = Field(default_factory=LlmConfig)
    hh: HhConfig = Field(default_factory=HhConfig)
    ton_jobs: TonJobsConfig = Field(default_factory=TonJobsConfig)


class EnvSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    telegram_bot_token: Optional[str] = None
    openai_api_key: Optional[str] = None
    openai_model: str = "gpt-4.1-mini"
    hh_user_agent: Optional[str] = None
    resume_text: Optional[str] = None
    resume_path: Path = Path("resume.md")
    config_path: Path = Path("config.yaml")
    database_path: Path = Path("data/processed.sqlite3")


def load_config() -> tuple[EnvSettings, AppConfig]:
    load_dotenv()
    env = EnvSettings()
    with env.config_path.open("r", encoding="utf-8") as file:
        raw_config: dict[str, Any] = yaml.safe_load(file) or {}
    return env, AppConfig.model_validate(raw_config)


def load_resume_text(env: EnvSettings) -> Optional[str]:
    if env.resume_text and env.resume_text.strip():
        return env.resume_text.strip()
    if env.resume_path.exists():
        text = env.resume_path.read_text(encoding="utf-8").strip()
        return text or None
    return None
