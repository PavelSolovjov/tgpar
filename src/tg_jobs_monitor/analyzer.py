from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Optional

from openai import AsyncOpenAI

from tg_jobs_monitor.settings import AppConfig, EnvSettings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AnalysisResult:
    accepted: bool
    is_vacancy: bool
    role_match: bool
    domain_match: bool
    level_match: bool
    reason: str
    matched_role: Optional[str] = None
    matched_domain: Optional[str] = None
    matched_level: Optional[str] = None


class VacancyAnalyzer:
    def __init__(self, env: EnvSettings, config: AppConfig) -> None:
        self.env = env
        self.config = config
        self.client = AsyncOpenAI(api_key=env.openai_api_key) if env.openai_api_key else None

    async def analyze(self, text: str) -> AnalysisResult:
        if self.client and self.config.llm.enabled:
            try:
                return await self._analyze_with_llm(text)
            except Exception:
                logger.exception("LLM analysis failed, falling back to keyword heuristic")
        return self._analyze_with_keywords(text)

    async def _analyze_with_llm(self, text: str) -> AnalysisResult:
        criteria = self.config.criteria
        trimmed_text = text[: self.config.llm.max_input_chars]
        prompt = {
            "task": "Analyze a Telegram post and decide whether it is a matching job vacancy.",
            "rules": [
                "Do not invent missing information.",
                "Accept only if the post is a job vacancy and matches role, domain, and level.",
                "If the level is not explicitly stated but the text clearly implies middle/senior experience, explain that inference.",
                "Reject junior, intern, lead-only, product-only, sales-only, recruiter-only, and unrelated posts.",
                "Return only valid JSON.",
            ],
            "criteria": {
                "roles": criteria.roles,
                "domains": criteria.domains,
                "levels": criteria.levels,
            },
            "required_json_schema": {
                "accepted": "boolean",
                "is_vacancy": "boolean",
                "role_match": "boolean",
                "domain_match": "boolean",
                "level_match": "boolean",
                "reason": "short Russian explanation",
                "matched_role": "string or null",
                "matched_domain": "string or null",
                "matched_level": "string or null",
            },
            "post_text": trimmed_text,
        }
        response = await self.client.chat.completions.create(
            model=self.env.openai_model,
            temperature=self.config.llm.temperature,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": "You are a precise vacancy classification assistant.",
                },
                {
                    "role": "user",
                    "content": json.dumps(prompt, ensure_ascii=False),
                },
            ],
        )
        content = response.choices[0].message.content or "{}"
        data = json.loads(content)
        return AnalysisResult(
            accepted=bool(data.get("accepted")),
            is_vacancy=bool(data.get("is_vacancy")),
            role_match=bool(data.get("role_match")),
            domain_match=bool(data.get("domain_match")),
            level_match=bool(data.get("level_match")),
            reason=str(data.get("reason") or "LLM did not provide a reason"),
            matched_role=data.get("matched_role"),
            matched_domain=data.get("matched_domain"),
            matched_level=data.get("matched_level"),
        )

    def _analyze_with_keywords(self, text: str) -> AnalysisResult:
        normalized = normalize(text)
        vacancy_markers = [
            "вакансия",
            "ищем",
            "ищу",
            "hiring",
            "job",
            "позиция",
            "открыта роль",
            "open role",
        ]
        is_vacancy = any(marker in normalized for marker in vacancy_markers)
        role = find_match(normalized, self.config.criteria.roles)
        domain = find_match(normalized, self.config.criteria.domains)
        level = find_match(normalized, self.config.criteria.levels)
        accepted = bool(is_vacancy and role and domain and level)

        missing = []
        if not is_vacancy:
            missing.append("не похоже на вакансию")
        if not role:
            missing.append("нет подходящей роли")
        if not domain:
            missing.append("нет подходящей сферы")
        if not level:
            missing.append("нет middle/senior уровня")

        reason = (
            f"Принято эвристикой: роль={role}, сфера={domain}, уровень={level}."
            if accepted
            else "Отклонено эвристикой: " + "; ".join(missing) + "."
        )
        return AnalysisResult(
            accepted=accepted,
            is_vacancy=is_vacancy,
            role_match=role is not None,
            domain_match=domain is not None,
            level_match=level is not None,
            reason=reason,
            matched_role=role,
            matched_domain=domain,
            matched_level=level,
        )


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.casefold()).strip()


def find_match(normalized_text: str, terms: list[str]) -> Optional[str]:
    for term in terms:
        if normalize(term) in normalized_text:
            return term
    return None
