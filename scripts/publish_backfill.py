from __future__ import annotations

import asyncio
import os
import sqlite3

from dotenv import load_dotenv

from tg_jobs_monitor.bot_publisher import BotPublisher
from tg_jobs_monitor.web_scraper import TelegramWebScraper


async def main() -> None:
    load_dotenv()
    connection = sqlite3.connect("data/processed.sqlite3")
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        """
        select source, message_id, reason
        from processed_messages
        where accepted = 1 and forwarded_message_id is null
        order by processed_at
        """
    ).fetchall()

    scraper = TelegramWebScraper()
    publisher = BotPublisher(os.environ["TELEGRAM_BOT_TOKEN"])
    try:
        sent = 0
        for row in rows:
            source = row["source"]
            message_id = row["message_id"]
            posts = await scraper.fetch_latest_posts(source, 50)
            post = next((item for item in posts if item.message_id == message_id), None)
            if post is None:
                print(f"SKIP not found {source}/{message_id}")
                continue

            reason = row["reason"].replace(
                " Не опубликовано: первый проход, publish_on_first_run=false.",
                "",
            )
            header = (
                "Подходящая вакансия\n"
                f"Источник: {post.source}\n"
                f"Причина: {reason}\n"
                f"Ссылка: {post.url}"
            )
            forwarded_id = await publisher.send_text(
                "@vakansiiprojectov",
                f"{header}\n\n{post.text}",
            )
            connection.execute(
                """
                update processed_messages
                set forwarded_message_id = ?, reason = ?
                where source = ? and message_id = ?
                """,
                (forwarded_id, reason, source, message_id),
            )
            connection.commit()
            sent += 1
            print(f"SENT {source}/{message_id} -> {forwarded_id}")

        print(f"DONE sent={sent} total={len(rows)}")
    finally:
        await scraper.close()
        await publisher.close()
        connection.close()


if __name__ == "__main__":
    asyncio.run(main())
