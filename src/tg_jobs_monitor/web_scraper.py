from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScrapedPost:
    source: str
    message_id: int
    text: str
    url: str
    datetime: Optional[str] = None


class TelegramWebScraper:
    def __init__(self) -> None:
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

    async def fetch_latest_posts(self, source: str, limit: int) -> list[ScrapedPost]:
        channel = normalize_channel(source)
        url = f"https://t.me/s/{channel}"
        response = await self.client.get(url)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        posts = []
        for message in soup.select(".tgme_widget_message.js-widget_message"):
            data_post = message.get("data-post", "")
            match = re.fullmatch(r"([^/]+)/(\d+)", data_post)
            if not match:
                continue

            message_text = message.select_one(".tgme_widget_message_text")
            if message_text is None:
                continue

            text = message_text.get_text(separator="\n", strip=True)
            if not text:
                continue

            message_id = int(match.group(2))
            date_node = message.select_one(".tgme_widget_message_date")
            time_node = message.select_one("time")
            post_url = (
                date_node.get("href")
                if date_node is not None and date_node.get("href")
                else f"https://t.me/{channel}/{message_id}"
            )
            posts.append(
                ScrapedPost(
                    source=f"@{channel}",
                    message_id=message_id,
                    text=text,
                    url=post_url,
                    datetime=time_node.get("datetime") if time_node is not None else None,
                )
            )

        posts.sort(key=lambda post: post.message_id)
        if limit > 0:
            posts = posts[-limit:]
        logger.info("Fetched %s posts from @%s", len(posts), channel)
        return posts


def normalize_channel(source: str) -> str:
    source = source.strip()
    if source.startswith("https://t.me/s/"):
        return source.removeprefix("https://t.me/s/").strip("/")
    if source.startswith("https://t.me/"):
        return source.removeprefix("https://t.me/").strip("/")
    return source.removeprefix("@").strip("/")
