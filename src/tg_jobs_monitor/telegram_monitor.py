from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Optional

from telethon import TelegramClient
from telethon.tl.custom.message import Message

from tg_jobs_monitor.analyzer import VacancyAnalyzer
from tg_jobs_monitor.settings import AppConfig, EnvSettings
from tg_jobs_monitor.storage import ProcessedRecord, Storage

logger = logging.getLogger(__name__)


class TelegramJobsMonitor:
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
        self.source_client = TelegramClient(
            env.telegram_session_name,
            env.telegram_api_id,
            env.telegram_api_hash,
        )
        self.bot_client = TelegramClient(
            env.bot_session_name,
            env.telegram_api_id,
            env.telegram_api_hash,
        )

    async def run_forever(self) -> None:
        async with self.source_client:
            bot_started = await self._start_bot_if_needed()
            try:
                logger.info("Monitor started. Sources: %s", ", ".join(self.config.source_channels))
                while True:
                    await self.poll_once()
                    await asyncio.sleep(self.config.poll_interval_seconds)
            finally:
                if bot_started:
                    await self.bot_client.disconnect()

    async def run_once(self) -> None:
        async with self.source_client:
            bot_started = await self._start_bot_if_needed()
            try:
                await self.poll_once()
            finally:
                if bot_started:
                    await self.bot_client.disconnect()

    async def _start_bot_if_needed(self) -> bool:
        if self.config.dry_run or self.config.forward_with_user:
            return False
        if not self.env.telegram_bot_token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is required when publishing with the bot")
        await self.bot_client.start(bot_token=self.env.telegram_bot_token)
        return True

    async def poll_once(self) -> None:
        for source in self.config.source_channels:
            try:
                await self._poll_source(source)
            except Exception:
                logger.exception("Failed to poll source %s", source)

    async def _poll_source(self, source: str) -> None:
        last_id = self.storage.last_message_id(source)
        kwargs = {"reverse": True}
        if last_id is None:
            kwargs["limit"] = self.config.recent_messages_limit
            logger.info("First pass for %s: checking last %s messages", source, kwargs["limit"])
        else:
            kwargs["min_id"] = last_id

        async for message in self.source_client.iter_messages(source, **kwargs):
            await self._process_message(source, message)

    async def _process_message(self, source: str, message: Message) -> None:
        if self.storage.is_message_processed(source, message.id):
            return

        text = (message.raw_text or "").strip()
        text_hash = hash_text(text)
        if not text:
            self._save_rejected(source, message.id, text_hash, "Отклонено: пустой текст сообщения.")
            return

        if self.storage.is_text_hash_processed(text_hash):
            self._save_rejected(
                source,
                message.id,
                text_hash,
                "Отклонено: текст уже обрабатывался ранее в другом сообщении.",
            )
            return

        result = await self.analyzer.analyze(text)
        logger.info(
            "%s/%s: %s | accepted=%s",
            source,
            message.id,
            result.reason,
            result.accepted,
        )

        forwarded_message_id = None
        if result.accepted and not self.config.dry_run:
            forwarded_message_id = await self._publish_match(source, message, result.reason)

        self.storage.save(
            ProcessedRecord(
                source=source,
                message_id=message.id,
                text_hash=text_hash,
                accepted=result.accepted,
                reason=result.reason,
                forwarded_message_id=forwarded_message_id,
            )
        )

    async def _publish_match(self, source: str, message: Message, reason: str) -> Optional[int]:
        if self.config.forward_with_user:
            forwarded = await self.source_client.forward_messages(
                self.config.destination_channel,
                message,
                from_peer=source,
            )
            return forwarded.id if forwarded else None

        link = build_message_link(source, message.id)
        body = message.raw_text or ""
        header = f"Подходящая вакансия\nИсточник: {source}\nПричина: {reason}"
        if link:
            header += f"\nСсылка: {link}"

        sent_messages = []
        for index, chunk in enumerate(split_telegram_text(f"{header}\n\n{body}")):
            prefix = f"Часть {index + 1}\n\n" if index > 0 else ""
            sent = await self.bot_client.send_message(
                self.config.destination_channel,
                f"{prefix}{chunk}",
                link_preview=False,
            )
            if sent:
                sent_messages.append(sent)
        return sent_messages[0].id if sent_messages else None

    def _save_rejected(self, source: str, message_id: int, text_hash: str, reason: str) -> None:
        logger.info("%s/%s: %s", source, message_id, reason)
        self.storage.save(
            ProcessedRecord(
                source=source,
                message_id=message_id,
                text_hash=text_hash,
                accepted=False,
                reason=reason,
            )
        )


def hash_text(text: str) -> str:
    normalized = " ".join(text.casefold().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def build_message_link(source: str, message_id: int) -> Optional[str]:
    if source.startswith("@"):
        return f"https://t.me/{source[1:]}/{message_id}"
    if source.startswith("https://t.me/"):
        return f"{source.rstrip('/')}/{message_id}"
    return None


def split_telegram_text(text: str, limit: int = 3900) -> list[str]:
    if len(text) <= limit:
        return [text]

    chunks = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break

        split_at = remaining.rfind("\n\n", 0, limit)
        if split_at < limit // 2:
            split_at = remaining.rfind("\n", 0, limit)
        if split_at < limit // 2:
            split_at = limit

        chunks.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()
    return chunks
