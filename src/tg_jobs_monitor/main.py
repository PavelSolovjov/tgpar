from __future__ import annotations

import asyncio
import argparse
import logging

from tg_jobs_monitor.analyzer import VacancyAnalyzer
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
    monitor = TelegramWebJobsMonitor(env, config, storage, analyzer)
    try:
        if args.once:
            asyncio.run(monitor.run_once())
        else:
            asyncio.run(monitor.run_forever())
    finally:
        storage.close()


if __name__ == "__main__":
    main()
