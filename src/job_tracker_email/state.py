from __future__ import annotations

import sqlite3
from pathlib import Path
from types import TracebackType


class SqliteTrackerState:
    def __init__(self, database_path: Path) -> None:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(database_path)
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS tracker_settings (
                setting_key TEXT PRIMARY KEY,
                setting_value TEXT NOT NULL
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS processed_messages (
                gmail_message_id TEXT PRIMARY KEY
            )
            """
        )
        self._connection.commit()

    def get_spreadsheet_id(self) -> str | None:
        row = self._connection.execute(
            """
            SELECT setting_value
            FROM tracker_settings
            WHERE setting_key = 'spreadsheet_id'
            """
        ).fetchone()
        return None if row is None else str(row[0])

    def save_spreadsheet_id(self, spreadsheet_id: str) -> None:
        self._connection.execute(
            """
            INSERT INTO tracker_settings (setting_key, setting_value)
            VALUES ('spreadsheet_id', ?)
            ON CONFLICT(setting_key) DO UPDATE
            SET setting_value = excluded.setting_value
            """,
            (spreadsheet_id,),
        )
        self._connection.commit()

    def get_successful_checkpoint(self) -> str | None:
        row = self._connection.execute(
            """
            SELECT setting_value
            FROM tracker_settings
            WHERE setting_key = 'successful_checkpoint'
            """
        ).fetchone()
        return None if row is None else str(row[0])

    def has_processed_message(self, message_id: str) -> bool:
        row = self._connection.execute(
            """
            SELECT 1
            FROM processed_messages
            WHERE gmail_message_id = ?
            """,
            (message_id,),
        ).fetchone()
        return row is not None

    def record_successful_sync(
        self,
        checkpoint: str,
        message_ids: tuple[str, ...],
    ) -> None:
        with self._connection:
            self._connection.executemany(
                """
                INSERT OR IGNORE INTO processed_messages (gmail_message_id)
                VALUES (?)
                """,
                ((message_id,) for message_id in message_ids),
            )
            self._connection.execute(
                """
                INSERT INTO tracker_settings (setting_key, setting_value)
                VALUES ('successful_checkpoint', ?)
                ON CONFLICT(setting_key) DO UPDATE
                SET setting_value = excluded.setting_value
                """,
                (checkpoint,),
            )

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> SqliteTrackerState:
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
