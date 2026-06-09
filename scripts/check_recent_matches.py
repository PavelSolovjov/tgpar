from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import yaml

from tg_jobs_monitor.analyzer import VacancyAnalyzer
from tg_jobs_monitor.settings import AppConfig
from tg_jobs_monitor.web_scraper import TelegramWebScraper


async def main(hours: int) -> None:
    now = datetime.now().astimezone()
    cutoff = now - timedelta(hours=hours)

    with open("config.yaml", "r", encoding="utf-8") as file:
        config = AppConfig.model_validate(yaml.safe_load(file))
    config.llm.enabled = False

    analyzer = VacancyAnalyzer(
        SimpleNamespace(openai_api_key=None, openai_model="x"),
        config,
    )
    scraper = TelegramWebScraper()

    scanned = 0
    accepted: list[tuple[str, str, int, str, str, str]] = []
    try:
        for source in config.source_channels:
            posts = await scraper.fetch_latest_posts(source, config.recent_messages_limit)
            for post in posts:
                if not post.datetime:
                    continue
                post_dt = datetime.fromisoformat(post.datetime).astimezone(now.tzinfo)
                if post_dt < cutoff:
                    continue

                scanned += 1
                result = await analyzer.analyze(post.text)
                if result.accepted:
                    title = post.text.splitlines()[0][:120] if post.text else ""
                    accepted.append(
                        (
                            post_dt.isoformat(),
                            post.source,
                            post.message_id,
                            post.url,
                            title,
                            result.reason,
                        )
                    )
    finally:
        await scraper.close()

    print(f"cutoff={cutoff.isoformat()} scanned={scanned} accepted={len(accepted)}")
    for item in accepted:
        print("ACCEPT", *item)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=int, default=11)
    args = parser.parse_args()
    asyncio.run(main(args.hours))
