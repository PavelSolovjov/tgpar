from __future__ import annotations

import sqlite3
from difflib import SequenceMatcher
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
    vacancy_title: Optional[str] = None
    company_name: Optional[str] = None
    dedupe_key: Optional[str] = None


class Storage:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self._migrate()

    def _migrate(self) -> None:
        try:
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
                    vacancy_title text,
                    company_name text,
                    dedupe_key text,
                    processed_at text not null,
                    unique(source, message_id)
                );

                create index if not exists idx_processed_text_hash
                    on processed_messages(text_hash);

                create index if not exists idx_processed_dedupe_key
                    on processed_messages(dedupe_key);

                create table if not exists source_state (
                    source text primary key,
                    last_polled_at text
                );
                """
            )
            for statement in (
                "alter table processed_messages add column vacancy_title text",
                "alter table processed_messages add column company_name text",
                "alter table processed_messages add column dedupe_key text",
            ):
                try:
                    self.connection.execute(statement)
                except sqlite3.OperationalError:
                    pass
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

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

    def find_similar_vacancy(
        self,
        dedupe_key: Optional[str],
        *,
        limit: int = 300,
    ) -> Optional[sqlite3.Row]:
        candidate = normalize_dedupe_key(dedupe_key)
        if not candidate:
            return None

        rows = self.connection.execute(
            """
            select source, message_id, dedupe_key
            from processed_messages
            where dedupe_key is not null and dedupe_key != ''
            order by id desc
            limit ?
            """,
            (limit,),
        ).fetchall()
        for row in rows:
            existing = normalize_dedupe_key(row["dedupe_key"])
            if existing and are_similar_dedupe_keys(candidate, existing):
                return row
        return None

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
                vacancy_title,
                company_name,
                dedupe_key,
                processed_at
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.source,
                record.message_id,
                record.text_hash,
                int(record.accepted),
                record.reason,
                record.forwarded_message_id,
                record.vacancy_title,
                record.company_name,
                record.dedupe_key,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self.connection.commit()

    def last_polled_at(self, source: str) -> Optional[datetime]:
        row = self.connection.execute(
            "select last_polled_at from source_state where source = ?",
            (source,),
        ).fetchone()
        if row is None or row["last_polled_at"] is None:
            return None
        value = str(row["last_polled_at"]).strip()
        if not value:
            return None
        return datetime.fromisoformat(value)

    def mark_polled(self, source: str, polled_at: Optional[datetime] = None) -> None:
        timestamp = (polled_at or datetime.now(timezone.utc)).isoformat()
        self.connection.execute(
            """
            insert into source_state (source, last_polled_at)
            values (?, ?)
            on conflict(source) do update set last_polled_at = excluded.last_polled_at
            """,
            (source, timestamp),
        )
        self.connection.commit()


def normalize_dedupe_key(value: Optional[str]) -> str:
    if value is None:
        return ""
    return " ".join(str(value).casefold().split())


def are_similar_dedupe_keys(left: str, right: str) -> bool:
    if left == right:
        return True

    left_company, left_title = split_dedupe_key(left)
    right_company, right_title = split_dedupe_key(right)
    if not left_company or not right_company:
        return False

    company_ratio = SequenceMatcher(None, left_company, right_company).ratio()
    title_ratio = SequenceMatcher(None, left_title, right_title).ratio()
    return company_ratio >= 0.84 and title_ratio >= 0.72


def split_dedupe_key(value: str) -> tuple[str, str]:
    company, _, title = value.partition("||")
    return company.strip(), title.strip()
