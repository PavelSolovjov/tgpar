from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class ProcessedRecord:
    source: str
    message_id: int
    text_hash: str
    accepted: bool
    reason: str
    forwarded_message_id: Optional[int] = None


class Storage:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self._migrate()

    def _migrate(self) -> None:
        self.connection.executescript(
            """
            create table if not exists processed_messages (
                id integer primary key autoincrement,
                source text not null,
                message_id integer not null,
                text_hash text not null,
                accepted integer not null,
                reason text not null,
                forwarded_message_id integer,
                processed_at text not null,
                unique(source, message_id)
            );

            create index if not exists idx_processed_text_hash
                on processed_messages(text_hash);
            """
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def is_message_processed(self, source: str, message_id: int) -> bool:
        row = self.connection.execute(
            "select 1 from processed_messages where source = ? and message_id = ?",
            (source, message_id),
        ).fetchone()
        return row is not None

    def is_text_hash_processed(self, text_hash: str) -> bool:
        row = self.connection.execute(
            "select 1 from processed_messages where text_hash = ?",
            (text_hash,),
        ).fetchone()
        return row is not None

    def last_message_id(self, source: str) -> Optional[int]:
        row = self.connection.execute(
            "select max(message_id) as last_id from processed_messages where source = ?",
            (source,),
        ).fetchone()
        if row is None:
            return None
        return row["last_id"]

    def save(self, record: ProcessedRecord) -> None:
        self.connection.execute(
            """
            insert or ignore into processed_messages (
                source,
                message_id,
                text_hash,
                accepted,
                reason,
                forwarded_message_id,
                processed_at
            )
            values (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.source,
                record.message_id,
                record.text_hash,
                int(record.accepted),
                record.reason,
                record.forwarded_message_id,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self.connection.commit()
