from __future__ import annotations

import sqlite3
from pathlib import Path
from types import TracebackType

from job_tracker_email.sync import (
    Application,
    ApplicationStatus,
    PendingApplicationWrite,
    PendingStatusUpdate,
    ThreadApplication,
    parse_application_status,
)


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
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_application_writes (
                gmail_message_id TEXT PRIMARY KEY,
                company TEXT NOT NULL,
                position TEXT NOT NULL,
                application_date TEXT NOT NULL,
                status TEXT NOT NULL,
                stage TEXT NOT NULL,
                matching_rows_before_write INTEGER NOT NULL
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_status_updates (
                gmail_message_id TEXT PRIMARY KEY,
                row_number INTEGER NOT NULL,
                status TEXT NOT NULL
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS application_threads (
                gmail_thread_id TEXT PRIMARY KEY,
                application_row_number INTEGER NOT NULL
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS application_thread_identities (
                gmail_thread_id TEXT PRIMARY KEY,
                company TEXT NOT NULL,
                position TEXT NOT NULL,
                application_date TEXT NOT NULL
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

    def get_pending_application_write(
        self,
        message_id: str,
    ) -> PendingApplicationWrite | None:
        row = self._connection.execute(
            """
            SELECT
                company,
                position,
                application_date,
                status,
                stage,
                matching_rows_before_write
            FROM pending_application_writes
            WHERE gmail_message_id = ?
            """,
            (message_id,),
        ).fetchone()
        if row is None:
            return None
        if row[3] != "Active":
            raise RuntimeError("Pending Application has an invalid Status.")
        return PendingApplicationWrite(
            application=Application(
                company=str(row[0]),
                position=str(row[1]),
                application_date=str(row[2]),
                status="Active",
                stage=str(row[4]),
            ),
            matching_rows_before_write=int(row[5]),
        )

    def get_pending_status_update(
        self,
        message_id: str,
    ) -> PendingStatusUpdate | None:
        row = self._connection.execute(
            """
            SELECT row_number, status
            FROM pending_status_updates
            WHERE gmail_message_id = ?
            """,
            (message_id,),
        ).fetchone()
        if row is None:
            return None
        status = parse_application_status(str(row[1]))
        if status not in {"Rejected", "Offer", "Withdrawn"}:
            raise RuntimeError("Pending Status update has an invalid Status.")
        return PendingStatusUpdate(
            row_number=int(row[0]),
            status=status,
        )

    def get_application_for_thread(
        self,
        thread_id: str,
    ) -> ThreadApplication | None:
        row = self._connection.execute(
            """
            SELECT
                application_threads.application_row_number,
                application_thread_identities.company,
                application_thread_identities.position,
                application_thread_identities.application_date
            FROM application_threads
            JOIN application_thread_identities
            ON application_threads.gmail_thread_id =
                application_thread_identities.gmail_thread_id
            WHERE application_threads.gmail_thread_id = ?
            """,
            (thread_id,),
        ).fetchone()
        if row is None:
            return None
        return ThreadApplication(
            row_number=int(row[0]),
            company=str(row[1]),
            position=str(row[2]),
            application_date=str(row[3]),
        )

    def record_application_thread(
        self,
        thread_id: str,
        row_number: int,
        application: Application,
    ) -> None:
        if not thread_id:
            return
        self._connection.execute(
            """
            INSERT INTO application_threads (
                gmail_thread_id, application_row_number
            )
            VALUES (?, ?)
            ON CONFLICT(gmail_thread_id) DO UPDATE SET
                application_row_number = excluded.application_row_number
            """,
            (thread_id, row_number),
        )
        self._connection.execute(
            """
            INSERT INTO application_thread_identities (
                gmail_thread_id, company, position, application_date
            )
            VALUES (?, ?, ?, ?)
            ON CONFLICT(gmail_thread_id) DO UPDATE SET
                company = excluded.company,
                position = excluded.position,
                application_date = excluded.application_date
            """,
            (
                thread_id,
                application.company,
                application.position,
                application.application_date,
            ),
        )
        self._connection.commit()

    def clear_application_thread(self, thread_id: str) -> None:
        if not thread_id:
            return
        with self._connection:
            self._connection.execute(
                """
                DELETE FROM application_threads
                WHERE gmail_thread_id = ?
                """,
                (thread_id,),
            )
            self._connection.execute(
                """
                DELETE FROM application_thread_identities
                WHERE gmail_thread_id = ?
                """,
                (thread_id,),
            )

    def record_pending_application_write(
        self,
        message_id: str,
        application: Application,
        matching_rows_before_write: int,
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO pending_application_writes (
                gmail_message_id,
                company,
                position,
                application_date,
                status,
                stage,
                matching_rows_before_write
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(gmail_message_id) DO UPDATE SET
                company = excluded.company,
                position = excluded.position,
                application_date = excluded.application_date,
                status = excluded.status,
                stage = excluded.stage,
                matching_rows_before_write =
                    excluded.matching_rows_before_write
            """,
            (
                message_id,
                application.company,
                application.position,
                application.application_date,
                application.status,
                application.stage,
                matching_rows_before_write,
            ),
        )
        self._connection.commit()

    def record_pending_status_update(
        self,
        message_id: str,
        row_number: int,
        status: ApplicationStatus,
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO pending_status_updates (
                gmail_message_id, row_number, status
            )
            VALUES (?, ?, ?)
            ON CONFLICT(gmail_message_id) DO UPDATE SET
                row_number = excluded.row_number,
                status = excluded.status
            """,
            (message_id, row_number, status),
        )
        self._connection.commit()

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
            self._connection.executemany(
                """
                DELETE FROM pending_application_writes
                WHERE gmail_message_id = ?
                """,
                ((message_id,) for message_id in message_ids),
            )
            self._connection.executemany(
                """
                DELETE FROM pending_status_updates
                WHERE gmail_message_id = ?
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
