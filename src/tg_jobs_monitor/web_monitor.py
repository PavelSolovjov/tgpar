from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import re
from typing import Optional

from tg_jobs_monitor.analyzer import AnalysisResult, VacancyAnalyzer
from tg_jobs_monitor.bot_publisher import BotPublisher
from tg_jobs_monitor.settings import AppConfig, EnvSettings
from tg_jobs_monitor.storage import ProcessedRecord, Storage
from tg_jobs_monitor.web_scraper import ScrapedPost, TelegramWebScraper

logger = logging.getLogger(__name__)
MIN_PUBLISH_MATCH_PERCENTAGE = 56


class TelegramWebJobsMonitor:
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
        self.scraper = TelegramWebScraper()
        self.publisher = (
            BotPublisher(env.telegram_bot_token)
            if env.telegram_bot_token and not config.dry_run
            else None
        )

    async def run_forever(self) -> None:
        logger.info("Web monitor started. Sources: %s", ", ".join(self.config.source_channels))
        try:
            while True:
                await self.poll_once()
                await asyncio.sleep(self.config.poll_interval_seconds)
        finally:
            await self.close()

    async def run_once(self) -> None:
        try:
            await self.poll_once()
        finally:
            await self.close()

    async def close(self) -> None:
        await self.scraper.close()
        if self.publisher is not None:
            await self.publisher.close()

    async def poll_once(self) -> None:
        if not self.config.dry_run and self.publisher is None:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is required unless dry_run is true")

        total_sources = len(self.config.source_channels)
        for index, source in enumerate(self.config.source_channels, start=1):
            logger.info("Polling source %s/%s: %s", index, total_sources, source)
            if index > 1:
                delay = self._next_request_delay()
                if delay > 0:
                    logger.info("Sleeping %.2fs before polling %s", delay, source)
                    await asyncio.sleep(delay)
            try:
                await self._poll_source(source)
            except Exception:
                logger.exception("Failed to poll source %s", source)

    async def _poll_source(self, source: str) -> None:
        last_id = self.storage.last_message_id(source)
        posts = await self.scraper.fetch_latest_posts(source, self.config.recent_messages_limit)

        if last_id is None:
            logger.info(
                "First pass for %s: checking last %s posts; publish_on_first_run=%s",
                source,
                len(posts),
                self.config.publish_on_first_run,
            )
            posts_to_process = posts
            should_publish = self.config.publish_on_first_run
        else:
            posts_to_process = [post for post in posts if post.message_id > last_id]
            should_publish = True

        for post in posts_to_process:
            await self._process_post(post, should_publish=should_publish)

    async def _process_post(self, post: ScrapedPost, should_publish: bool) -> None:
        if self.storage.is_message_processed(post.source, post.message_id):
            return

        text_hash = hash_text(post.text)
        if self.storage.is_text_hash_processed(text_hash):
            self._save_rejected(
                post,
                text_hash,
                "Отклонено: текст уже обрабатывался ранее в другом сообщении.",
            )
            return

        result = await self.analyzer.analyze(post.text)
        dedupe_key = build_dedupe_key(post.text, result)
        duplicate = self.storage.find_similar_vacancy(dedupe_key)
        if duplicate is not None:
            self._save_rejected(
                post,
                text_hash,
                (
                    "Отклонено: похоже на дубль уже обработанной вакансии "
                    f"из {duplicate['source']}/{duplicate['message_id']}."
                ),
                result=result,
            )
            return

        reason = result.reason
        if result.accepted and not should_publish:
            reason += " Не опубликовано: первый проход, publish_on_first_run=false."
        elif result.accepted and not is_publishable_match(result):
            reason += (
                f" Не опубликовано: процент совпадения должен быть выше 55 "
                f"(сейчас {result.match_percentage if result.match_percentage is not None else 'не указан'}%)."
            )

        logger.info(
            "%s/%s: %s | accepted=%s",
            post.source,
            post.message_id,
            reason,
            result.accepted,
        )

        forwarded_message_id = None
        if result.accepted and should_publish and is_publishable_match(result) and not self.config.dry_run:
            forwarded_message_id = await self._publish_match(post, result, reason)

        self.storage.save(
            ProcessedRecord(
                source=post.source,
                message_id=post.message_id,
                text_hash=text_hash,
                accepted=result.accepted,
                reason=reason,
                forwarded_message_id=forwarded_message_id,
                vacancy_title=result.vacancy_title,
                company_name=result.company_name,
                dedupe_key=dedupe_key,
            )
        )

    async def _publish_match(
        self,
        post: ScrapedPost,
        result: AnalysisResult,
        reason: str,
    ) -> Optional[int]:
        if self.publisher is None:
            return None

        header = format_publication(post, result, reason)
        return await self.publisher.send_text(
            self.config.destination_channel,
            header,
        )

    def _save_rejected(
        self,
        post: ScrapedPost,
        text_hash: str,
        reason: str,
        *,
        result: Optional[AnalysisResult] = None,
    ) -> None:
        logger.info("%s/%s: %s", post.source, post.message_id, reason)
        self.storage.save(
            ProcessedRecord(
                source=post.source,
                message_id=post.message_id,
                text_hash=text_hash,
                accepted=False,
                reason=reason,
                vacancy_title=result.vacancy_title if result else None,
                company_name=result.company_name if result else None,
                dedupe_key=build_dedupe_key(post.text, result) if result else None,
            )
        )

    def _next_request_delay(self) -> float:
        base = max(0.0, self.config.request_delay_seconds)
        jitter = max(0.0, self.config.request_delay_jitter_seconds)
        if jitter == 0:
            return base
        return max(0.0, base + random.uniform(0, jitter))


def hash_text(text: str) -> str:
    normalized = " ".join(text.casefold().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def is_publishable_match(result: AnalysisResult) -> bool:
    return (
        result.match_percentage is not None
        and result.match_percentage >= MIN_PUBLISH_MATCH_PERCENTAGE
    )


def format_publication(
    post: ScrapedPost,
    result: AnalysisResult,
    reason: str,
) -> str:
    title = result.vacancy_title or first_non_empty_line(post.text) or "Подходящая вакансия"
    lines = [title]

    if result.company_name:
        lines.extend(["", f"🏢 Компания: {result.company_name}"])
        company_tag = classify_company_tag(result.company_name)
        if company_tag:
            lines.append(f"🏷️ Тип компании: {company_tag}")

    if result.domain_label or result.matched_domain:
        lines.extend(["", f"🌐 Домен: {result.domain_label or result.matched_domain}"])

    if result.match_percentage is not None:
        lines.append(f"📊 Процент совпадения: {result.match_percentage}%")

    salary = extract_salary_info(post.text)
    if salary:
        lines.append(f"💰 Зарплата: {salary}")

    lines.extend(["", f"🔗 Ссылка: {post.url}"])

    if result.responsibilities_summary:
        lines.extend(["", "🧩 Что предстоит делать:"])
        lines.extend(f"• {item}" for item in result.responsibilities_summary)
    elif result.resume_summary:
        lines.extend(["", "🧩 Что предстоит делать:", f"• {result.resume_summary}"])

    if result.mismatches:
        lines.extend(["", "⚠️ Несовпадения:"])
        lines.extend(f"- {item}" for item in result.mismatches)

    if not result.responsibilities_summary and not result.mismatches:
        lines.extend(["", f"💬 Комментарий: {reason}"])

    return "\n".join(lines).strip()


def first_non_empty_line(text: str) -> Optional[str]:
    for line in text.splitlines():
        cleaned = line.strip()
        if cleaned:
            return cleaned
    return None


def classify_company_tag(company_name: str) -> Optional[str]:
    normalized = " ".join(company_name.casefold().split())
    big_tech_keywords = [
        "втб",
        "vtb",
        "тинькофф",
        "т-банк",
        "t-bank",
        "tinkoff",
        "сбер",
        "sber",
        "сбербанк",
        "sberbank",
        "альфа-банк",
        "alfabank",
        "alpha bank",
        "газпромбанк",
        "gazprombank",
        "мтс",
        "mts",
        "ozon",
        "яндекс",
        "yandex",
        "vk",
    ]
    if any(keyword in normalized for keyword in big_tech_keywords):
        return "Big Tech / Enterprise"
    return None


def extract_salary_info(text: str) -> Optional[str]:
    currency_markers = ("руб", "rur", "usd", "eur", "$", "€", "k ", "k$", "k€", "₽")
    for raw_line in text.splitlines():
        line = " ".join(raw_line.strip().split())
        if not line:
            continue
        lower = line.casefold()
        if "зарплат" in lower or "salary" in lower or "compensation" in lower:
            return cleanup_salary_line(line)
        if any(marker in lower for marker in currency_markers) and re.search(r"\d", line):
            if re.search(r"(от|до|from|up to|\d[\d\s.,]{2,}\s*[-–—]\s*\d)", lower):
                return cleanup_salary_line(line)
    return None


def cleanup_salary_line(line: str) -> str:
    cleaned = re.sub(r"^(зарплата|salary|compensation)\s*[:\-]\s*", "", line, flags=re.IGNORECASE)
    return cleaned.strip(" -")


def build_dedupe_key(text: str, result: Optional[AnalysisResult]) -> Optional[str]:
    if result is None:
        return None

    company = normalize_company_name(result.company_name)
    title = normalize_title_for_dedupe(result.vacancy_title or first_non_empty_line(text))
    if not company and not title:
        return None
    return f"{company}||{title}"


def normalize_company_name(company_name: Optional[str]) -> str:
    if not company_name:
        return ""
    normalized = company_name.casefold()
    normalized = normalized.replace("ё", "е")
    normalized = re.sub(r"[^a-zа-я0-9\s]", " ", normalized)
    normalized = re.sub(r"\b(ооо|ao|ао|ип|llc|ltd|inc|gmbh|jsc)\b", " ", normalized)
    return " ".join(normalized.split())


def normalize_title_for_dedupe(title: Optional[str]) -> str:
    if not title:
        return ""
    normalized = title.casefold().replace("ё", "е")
    replacements = {
        "проджект менеджер": "project manager",
        "менеджер проектов": "project manager",
        "руководитель проектов": "project manager",
        "delivery manager": "project manager",
        "project manager": "project manager",
        "pm": "project manager",
    }
    for source, target in replacements.items():
        normalized = normalized.replace(source, target)
    normalized = re.sub(r"\b(project manager|в|at|in|for)\b", " ", normalized)
    normalized = re.sub(r"[^a-zа-я0-9\s]", " ", normalized)
    return " ".join(normalized.split())
