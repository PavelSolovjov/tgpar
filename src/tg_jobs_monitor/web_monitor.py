from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Optional

from tg_jobs_monitor.analyzer import VacancyAnalyzer
from tg_jobs_monitor.bot_publisher import BotPublisher
from tg_jobs_monitor.settings import AppConfig, EnvSettings
from tg_jobs_monitor.storage import ProcessedRecord, Storage
from tg_jobs_monitor.web_scraper import ScrapedPost, TelegramWebScraper

logger = logging.getLogger(__name__)


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

        for source in self.config.source_channels:
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
        reason = result.reason
        if result.accepted and not should_publish:
            reason += " Не опубликовано: первый проход, publish_on_first_run=false."

        logger.info(
            "%s/%s: %s | accepted=%s",
            post.source,
            post.message_id,
            reason,
            result.accepted,
        )

        forwarded_message_id = None
        if result.accepted and should_publish and not self.config.dry_run:
            forwarded_message_id = await self._publish_match(post, reason, result.resume_summary)

        self.storage.save(
            ProcessedRecord(
                source=post.source,
                message_id=post.message_id,
                text_hash=text_hash,
                accepted=result.accepted,
                reason=reason,
                forwarded_message_id=forwarded_message_id,
            )
        )

    async def _publish_match(
        self,
        post: ScrapedPost,
        reason: str,
        resume_summary: Optional[str],
    ) -> Optional[int]:
        if self.publisher is None:
            return None

        sections = ["Подходящая вакансия"]
        if resume_summary:
            sections.append(f"Саммари: {resume_summary}")
        sections.append(f"Источник: {post.source}")
        sections.append(f"Причина: {reason}")
        sections.append(f"Ссылка: {post.url}")
        header = "\n".join(sections)
        return await self.publisher.send_text(
            self.config.destination_channel,
            f"{header}\n\n{post.text}",
        )

    def _save_rejected(self, post: ScrapedPost, text_hash: str, reason: str) -> None:
        logger.info("%s/%s: %s", post.source, post.message_id, reason)
        self.storage.save(
            ProcessedRecord(
                source=post.source,
                message_id=post.message_id,
                text_hash=text_hash,
                accepted=False,
                reason=reason,
            )
        )


def hash_text(text: str) -> str:
    normalized = " ".join(text.casefold().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
