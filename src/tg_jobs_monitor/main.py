from __future__ import annotations

import asyncio
import argparse
import logging

from tg_jobs_monitor.analyzer import VacancyAnalyzer
from tg_jobs_monitor.hh_monitor import HhJobsMonitor
from tg_jobs_monitor.settings import load_config
from tg_jobs_monitor.storage import Storage
from tg_jobs_monitor.web_monitor import TelegramWebJobsMonitor


def main() -> None:
    parser = argparse.ArgumentParser(description="Monitor Telegram channels for matching job posts.")
    parser.add_argument(
        "--once",
        action="store_true",
        help="poll configured channels once and exit",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    env, config = load_config()
    storage = Storage(env.database_path)
    analyzer = VacancyAnalyzer(env, config)
    tg_monitor = TelegramWebJobsMonitor(env, config, storage, analyzer)
    hh_monitor = HhJobsMonitor(env, config, storage, analyzer)
    try:
        if args.once:
            asyncio.run(run_once(tg_monitor, hh_monitor))
        else:
            asyncio.run(run_forever(tg_monitor, hh_monitor, config.poll_interval_seconds))
    finally:
        storage.close()


async def run_once(
    tg_monitor: TelegramWebJobsMonitor,
    hh_monitor: HhJobsMonitor,
) -> None:
    await tg_monitor.run_once()
    await hh_monitor.run_once()


async def run_forever(
    tg_monitor: TelegramWebJobsMonitor,
    hh_monitor: HhJobsMonitor,
    poll_interval_seconds: int,
) -> None:
    try:
        while True:
            await tg_monitor.poll_once()
            await hh_monitor.poll_once()
            await asyncio.sleep(poll_interval_seconds)
    finally:
        await tg_monitor.close()
        await hh_monitor.close()


if __name__ == "__main__":
    main()
