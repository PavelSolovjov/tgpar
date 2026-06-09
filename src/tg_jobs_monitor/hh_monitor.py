from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import random
import re
from dataclasses import dataclass
from typing import Optional

import httpx

from tg_jobs_monitor.analyzer import AnalysisResult, VacancyAnalyzer
from tg_jobs_monitor.bot_publisher import BotPublisher
from tg_jobs_monitor.settings import AppConfig, EnvSettings, HhSearchConfig
from tg_jobs_monitor.storage import ProcessedRecord, Storage
from tg_jobs_monitor.web_monitor import format_publication

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HhVacancy:
    source: str
    message_id: int
    text: str
    url: str
    datetime: Optional[str] = None


class HhApiClient:
    def __init__(self, user_agent: str) -> None:
        self.client = httpx.AsyncClient(
            base_url="https://api.hh.ru",
            timeout=30,
            headers={
                "HH-User-Agent": user_agent,
                "Accept": "application/json",
            },
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def search_vacancies(
        self,
        search: HhSearchConfig,
        *,
        per_page: int,
        pages: int,
    ) -> list[dict]:
        items: list[dict] = []
        for page in range(max(1, pages)):
            params = {
                "text": search.text,
                "per_page": per_page,
                "page": page,
                "order_by": "publication_time",
            }
            if search.area is not None:
                params["area"] = search.area
            response = await self.client.get("/vacancies", params=params)
            response.raise_for_status()
            payload = response.json()
            page_items = payload.get("items", [])
            if not isinstance(page_items, list):
                break
            items.extend(page_items)
            if page + 1 >= int(payload.get("pages", 0) or 0):
                break
        return items

    async def fetch_vacancy(self, vacancy_id: int | str) -> dict:
        response = await self.client.get(f"/vacancies/{vacancy_id}")
        response.raise_for_status()
        return response.json()


class HhJobsMonitor:
    def __init__(
        self,
        env: EnvSettings,
        config: AppConfig,
        storage: Storage,
        analyzer: VacancyAnalyzer,
    ) -> None:
        self.env = env
        self.config = config
        self.storage = storage
        self.analyzer = analyzer
        self.client = (
            HhApiClient(env.hh_user_agent.strip())
            if env.hh_user_agent and env.hh_user_agent.strip()
            else None
        )
        self.publisher = (
            BotPublisher(env.telegram_bot_token)
            if env.telegram_bot_token and not config.dry_run
            else None
        )

    async def run_once(self) -> None:
        try:
            await self.poll_once()
        finally:
            await self.close()

    async def poll_once(self) -> None:
        if not self.config.hh.enabled:
            logger.info("HH monitor disabled in config.")
            return
        if self.client is None:
            logger.warning("HH monitor enabled, but HH_USER_AGENT is missing. Skipping HH polling.")
            return
        if not self.config.dry_run and self.publisher is None:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is required unless dry_run is true")

        total_searches = len(self.config.hh.searches)
        for index, search in enumerate(self.config.hh.searches, start=1):
            logger.info("Polling HH search %s/%s: %s", index, total_searches, search.source)
            if index > 1 and self.config.hh.request_delay_seconds > 0:
                delay = self.config.hh.request_delay_seconds + random.uniform(0, 0.5)
                logger.info("Sleeping %.2fs before HH search %s", delay, search.source)
                await asyncio.sleep(delay)
            await self._poll_search(search)

    async def close(self) -> None:
        if self.client is not None:
            await self.client.close()
        if self.publisher is not None:
            await self.publisher.close()

    async def _poll_search(self, search: HhSearchConfig) -> None:
        assert self.client is not None
        source = f"hh:{search.source}"
        last_id = self.storage.last_message_id(source)
        items = await self.client.search_vacancies(
            search,
            per_page=self.config.hh.per_page,
            pages=self.config.hh.pages,
        )

        if last_id is None:
            logger.info(
                "First HH pass for %s: checking %s vacancies; publish_on_first_run=%s",
                source,
                len(items),
                self.config.publish_on_first_run,
            )
            should_publish = self.config.publish_on_first_run
        else:
            should_publish = True

        for item in items:
            vacancy_id = int(item["id"])
            if self.storage.is_message_processed(source, vacancy_id):
                continue
            details = await self.client.fetch_vacancy(vacancy_id)
            vacancy = self._build_vacancy(source, details)
            await self._process_vacancy(vacancy, should_publish=should_publish)

    async def _process_vacancy(self, vacancy: HhVacancy, should_publish: bool) -> None:
        text_hash = hash_text(vacancy.text)
        if self.storage.is_text_hash_processed(text_hash):
            self._save_rejected(
                vacancy,
                text_hash,
                "Отклонено: текст уже обрабатывался ранее в другом сообщении.",
            )
            return

        prefilter_reason = self._hh_prefilter_reason(vacancy)
        if prefilter_reason:
            self._save_rejected(vacancy, text_hash, prefilter_reason)
            return

        result = await self.analyzer.analyze(vacancy.text)
        reason = result.reason
        if result.accepted and not should_publish:
            reason += " Не опубликовано: первый проход, publish_on_first_run=false."

        logger.info(
            "%s/%s: %s | accepted=%s",
            vacancy.source,
            vacancy.message_id,
            reason,
            result.accepted,
        )

        forwarded_message_id = None
        if result.accepted and should_publish and not self.config.dry_run:
            forwarded_message_id = await self._publish_match(vacancy, result, reason)

        self.storage.save(
            ProcessedRecord(
                source=vacancy.source,
                message_id=vacancy.message_id,
                text_hash=text_hash,
                accepted=result.accepted,
                reason=reason,
                forwarded_message_id=forwarded_message_id,
            )
        )

    async def _publish_match(
        self,
        vacancy: HhVacancy,
        result: AnalysisResult,
        reason: str,
    ) -> Optional[int]:
        if self.publisher is None:
            return None
        text = format_publication(vacancy, result, reason)
        return await self.publisher.send_text(self.config.destination_channel, text)

    def _save_rejected(self, vacancy: HhVacancy, text_hash: str, reason: str) -> None:
        logger.info("%s/%s: %s", vacancy.source, vacancy.message_id, reason)
        self.storage.save(
            ProcessedRecord(
                source=vacancy.source,
                message_id=vacancy.message_id,
                text_hash=text_hash,
                accepted=False,
                reason=reason,
            )
        )

    def _build_vacancy(self, source: str, details: dict) -> HhVacancy:
        title = str(details.get("name") or "").strip()
        company = ((details.get("employer") or {}).get("name") or "").strip()
        experience = ((details.get("experience") or {}).get("name") or "").strip()
        schedule = ((details.get("schedule") or {}).get("name") or "").strip()
        employment = ((details.get("employment") or {}).get("name") or "").strip()
        area = ((details.get("area") or {}).get("name") or "").strip()
        description = clean_html(details.get("description") or "")
        key_skills = ", ".join(skill.get("name", "").strip() for skill in details.get("key_skills") or [] if skill.get("name"))
        salary = format_salary(details.get("salary"))

        chunks = [title]
        if company:
            chunks.append(f"Компания: {company}")
        if salary:
            chunks.append(f"Зарплата: {salary}")
        if experience:
            chunks.append(f"Опыт: {experience}")
        if schedule:
            chunks.append(f"Формат: {schedule}")
        if employment:
            chunks.append(f"Занятость: {employment}")
        if area:
            chunks.append(f"Локация: {area}")
        if key_skills:
            chunks.append(f"Ключевые навыки: {key_skills}")
        if description:
            chunks.append("")
            chunks.append(description)

        return HhVacancy(
            source=source,
            message_id=int(details["id"]),
            text="\n".join(part for part in chunks if part).strip(),
            url=str(details.get("alternate_url") or f"https://hh.ru/vacancy/{details['id']}"),
            datetime=details.get("published_at"),
        )

    def _hh_prefilter_reason(self, vacancy: HhVacancy) -> Optional[str]:
        normalized = normalize(vacancy.text)
        salary_reason = salary_filter_reason(vacancy.text, self.config.hh.min_salary_rur)
        if salary_reason:
            return salary_reason

        if self.config.hh.require_remote and not supports_remote(normalized):
            return "Отклонено HH-фильтром: вакансия не поддерживает удаленную работу."

        if self.config.hh.allowed_domain_keywords and not has_any_keyword(
            normalized,
            self.config.hh.allowed_domain_keywords,
        ):
            return "Отклонено HH-фильтром: вакансия не попадает в домены crypto/web3/fintech/mobile."

        if self.config.hh.excluded_grade_terms and has_any_keyword(
            normalized,
            self.config.hh.excluded_grade_terms,
        ):
            return "Отклонено HH-фильтром: junior/junior-профиль."

        if self.config.hh.excluded_company_keywords and has_any_keyword(
            normalized,
            self.config.hh.excluded_company_keywords,
        ):
            return "Отклонено HH-фильтром: компания похожа на заказную разработку или аутсорс."

        return None


def clean_html(value: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
    text = re.sub(r"</p\s*>", "\n\n", text, flags=re.I)
    text = re.sub(r"<li\s*>", "• ", text, flags=re.I)
    text = re.sub(r"</li\s*>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return text.strip()


def format_salary(payload: object) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    salary_from = payload.get("from")
    salary_to = payload.get("to")
    currency = payload.get("currency")
    gross = payload.get("gross")

    parts = []
    if salary_from:
        parts.append(f"от {salary_from}")
    if salary_to:
        parts.append(f"до {salary_to}")
    if currency:
        parts.append(str(currency))
    if gross is True:
        parts.append("gross")
    elif gross is False:
        parts.append("net")
    if not parts:
        return None
    return " ".join(parts)


def salary_filter_reason(text: str, min_salary_rur: int) -> Optional[str]:
    if min_salary_rur <= 0:
        return None
    match = re.search(r"зарплата:\s*(.+)", text, flags=re.I)
    if not match:
        return None
    salary_text = match.group(1).strip()
    if "RUR" not in salary_text and "руб" not in salary_text:
        return None

    numbers = [int(value) for value in re.findall(r"\d+", salary_text)]
    if not numbers:
        return None
    max_value = max(numbers)
    if max_value < min_salary_rur:
        return f"Отклонено HH-фильтром: зарплата ниже {min_salary_rur} рублей."
    return None


def supports_remote(normalized_text: str) -> bool:
    remote_keywords = [
        "удален",
        "дистанцион",
        "remote",
        "home office",
        "гибрид",
        "hybrid",
    ]
    return has_any_keyword(normalized_text, remote_keywords)


def has_any_keyword(normalized_text: str, keywords: list[str]) -> bool:
    for keyword in keywords:
        if normalize(keyword) in normalized_text:
            return True
    return False


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.casefold()).strip()


def hash_text(text: str) -> str:
    normalized = " ".join(text.casefold().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
