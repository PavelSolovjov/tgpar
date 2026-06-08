from __future__ import annotations

import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


class BotPublisher:
    def __init__(self, bot_token: str) -> None:
        self.bot_token = bot_token
        self.client = httpx.AsyncClient(timeout=30)

    async def close(self) -> None:
        await self.client.aclose()

    async def send_text(self, destination_channel: str, text: str) -> Optional[int]:
        first_message_id = None
        for chunk in split_telegram_text(text):
            response = await self.client.post(
                f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                json={
                    "chat_id": destination_channel,
                    "text": chunk,
                    "disable_web_page_preview": True,
                },
            )
            response.raise_for_status()
            payload = response.json()
            if not payload.get("ok"):
                raise RuntimeError(f"Telegram Bot API error: {payload}")
            message_id = payload["result"]["message_id"]
            if first_message_id is None:
                first_message_id = message_id
        return first_message_id


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
