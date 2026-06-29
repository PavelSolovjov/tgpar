from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from tg_jobs_monitor.analyzer import AnalysisResult, VacancyAnalyzer
from tg_jobs_monitor.bot_publisher import BotPublisher
from tg_jobs_monitor.settings import AppConfig, EnvSettings
from tg_jobs_monitor.storage import ProcessedRecord, Storage
from tg_jobs_monitor.web_monitor import build_dedupe_key, format_publication, is_publishable_match

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TonJob:
    source: str
    message_id: int
    text: str
    url: str
    datetime: str | None = None


class TonJobsScraper:
    def __init__(self, jobs_url: str, max_description_chars: int) -> None:
        self.jobs_url = jobs_url
        self.max_description_chars = max_description_chars
        self.client = httpx.AsyncClient(
            timeout=30,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
                )
            },
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def fetch_latest_jobs(self) -> list[TonJob]:
        response = await self.client.get(self.jobs_url)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        jobs: dict[int, TonJob] = {}
        for anchor in soup.select('a[href*="/jobs/"]'):
            href = anchor.get("href") or ""
            match = re.search(r"/jobs/(\d+)", href)
            if not match:
                continue

            title = anchor.get_text(" ", strip=True)
            if not title or title.casefold() in {"view job", "search jobs", "jobs"}:
                continue

            job_id = int(match.group(1))
            absolute_url = urljoin(self.jobs_url, href)
            card = anchor.find_parent(["article", "li", "div", "section"])
            card_text = card.get_text("\n", strip=True) if card is not None else title
            normalized_card_text = normalize_whitespace(card_text)

            current = jobs.get(job_id)
            if current is not None and len(current.text) >= len(normalized_card_text):
                continue

            jobs[job_id] = TonJob(
                source="ton-jobs",
                message_id=job_id,
                text=normalized_card_text,
                url=absolute_url,
            )

        job_list = sorted(jobs.values(), key=lambda job: job.message_id)
        logger.info("Fetched %s jobs from TON job board", len(job_list))
        return job_list

    async def enrich_job(self, job: TonJob) -> TonJob:
        response = await self.client.get(job.url)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        root = soup.select_one("main") or soup.body
        if root is None:
            return job

        parts: list[str] = []
        title = first_text(
            soup.select_one("main h1"),
            soup.select_one("main h2"),
            soup.select_one("h1"),
        )
        if title:
            parts.append(title)

        listing_excerpt = job.text.strip()
        if listing_excerpt:
            parts.append(listing_excerpt)

        detail_text = normalize_whitespace(root.get_text("\n", strip=True))
        if detail_text and detail_text not in parts:
            parts.append(detail_text)

        text = "\n\n".join(part for part in parts if part).strip()
        if self.max_description_chars > 0:
            text = text[: self.max_description_chars]
        return TonJob(
            source=job.source,
            message_id=job.message_id,
            text=text or job.text,
            url=job.url,
            datetime=job.datetime,
        )


class TonJobsMonitor:
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
        self.scraper = TonJobsScraper(
            jobs_url=config.ton_jobs.jobs_url,
            max_description_chars=config.ton_jobs.max_description_chars,
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

    async def close(self) -> None:
        await self.scraper.close()
        if self.publisher is not None:
            await self.publisher.close()

    async def poll_once(self) -> None:
        if not self.config.ton_jobs.enabled:
            logger.info("TON jobs monitor disabled in config.")
            return
        if not self.config.dry_run and self.publisher is None:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is required unless dry_run is true")

        source = "ton-jobs"
        if self._should_skip_poll(source):
            return

        last_id = self.storage.last_message_id(source)
        jobs = await self.scraper.fetch_latest_jobs()
        self.storage.mark_polled(source)

        if last_id is None:
            logger.info(
                "First TON jobs pass: checking %s jobs; publish_on_first_run=%s",
                len(jobs),
                self.config.publish_on_first_run,
            )
            jobs_to_process = jobs
            should_publish = self.config.publish_on_first_run
        else:
            jobs_to_process = [job for job in jobs if job.message_id > last_id]
            should_publish = True

        total_jobs = len(jobs_to_process)
        for index, job in enumerate(jobs_to_process, start=1):
            logger.info("Processing TON job %s/%s: %s", index, total_jobs, job.url)
            if index > 1 and self.config.ton_jobs.request_delay_seconds > 0:
                await asyncio.sleep(self.config.ton_jobs.request_delay_seconds)
            try:
                enriched = await self.scraper.enrich_job(job)
                await self._process_job(enriched, should_publish=should_publish)
            except Exception:
                logger.exception("Failed to process TON job %s", job.url)

    def _should_skip_poll(self, source: str) -> bool:
        min_interval_hours = max(0.0, self.config.ton_jobs.min_poll_interval_hours)
        if min_interval_hours <= 0:
            return False

        last_polled_at = self.storage.last_polled_at(source)
        if last_polled_at is None:
            return False

        now = datetime.now(timezone.utc)
        next_allowed_at = last_polled_at + timedelta(hours=min_interval_hours)
        if next_allowed_at > now:
            remaining = next_allowed_at - now
            logger.info(
                "Skipping TON jobs poll for %s. Next check available in %.1f hours.",
                source,
                remaining.total_seconds() / 3600,
            )
            return True
        return False

    async def _process_job(self, job: TonJob, should_publish: bool) -> None:
        if self.storage.is_message_processed(job.source, job.message_id):
            return

        text_hash = hash_text(job.text)
        if self.storage.is_text_hash_processed(text_hash):
            self._save_rejected(
                job,
                text_hash,
                "Отклонено: текст уже обрабатывался ранее в другом сообщении.",
            )
            return

        result = await self.analyzer.analyze(job.text)
        dedupe_key = build_dedupe_key(job.text, result)
        duplicate = self.storage.find_similar_vacancy(dedupe_key)
        if duplicate is not None:
            self._save_rejected(
                job,
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

        logger.info("%s/%s: %s | accepted=%s", job.source, job.message_id, reason, result.accepted)

        forwarded_message_id = None
        if result.accepted and should_publish and is_publishable_match(result) and not self.config.dry_run:
            forwarded_message_id = await self._publish_match(job, result, reason)

        self.storage.save(
            ProcessedRecord(
                source=job.source,
                message_id=job.message_id,
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
        job: TonJob,
        result: AnalysisResult,
        reason: str,
    ) -> int | None:
        if self.publisher is None:
            return None
        text = format_publication(job, result, reason)
        return await self.publisher.send_text(self.config.destination_channel, text)

    def _save_rejected(
        self,
        job: TonJob,
        text_hash: str,
        reason: str,
        *,
        result: AnalysisResult | None = None,
    ) -> None:
        logger.info("%s/%s: %s", job.source, job.message_id, reason)
        self.storage.save(
            ProcessedRecord(
                source=job.source,
                message_id=job.message_id,
                text_hash=text_hash,
                accepted=False,
                reason=reason,
                vacancy_title=result.vacancy_title if result else None,
                company_name=result.company_name if result else None,
                dedupe_key=build_dedupe_key(job.text, result) if result else None,
            )
        )


def normalize_whitespace(text: str) -> str:
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return text.strip()


def first_text(*nodes: object) -> str | None:
    for node in nodes:
        if node is None:
            continue
        if not hasattr(node, "get_text"):
            continue
        text = normalize_whitespace(node.get_text(" ", strip=True))
        if text:
            return text
    return None


def hash_text(text: str) -> str:
    normalized = " ".join(text.casefold().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
