from __future__ import annotations

import base64
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from job_tracker_email.sync import (
    Application,
    GmailMessage,
    MailboxScan,
    NeedsReview,
)


GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"


@dataclass(frozen=True)
class GoogleAuthConfig:
    client_secrets_path: Path
    token_path: Path
    scopes: tuple[str, ...] = (GMAIL_READONLY_SCOPE, DRIVE_FILE_SCOPE)


@dataclass(frozen=True)
class TrackerSpreadsheet:
    title: str
    applications_columns: tuple[str, ...]
    additional_tabs: tuple[str, ...]


GoogleServiceFactory = Callable[[str, str], Any]


class GoogleApiWorkspace:
    """Gmail and Sheets boundary backed by one installed-app OAuth grant."""

    def __init__(
        self,
        config: GoogleAuthConfig,
        *,
        service_factory: GoogleServiceFactory | None = None,
    ) -> None:
        self._config = config
        self._service_factory = service_factory

    def create_spreadsheet(
        self, definition: TrackerSpreadsheet
    ) -> str:
        service = self._build_google_service("sheets", "v4")
        response = (
            service.spreadsheets()
            .create(
                body=self._spreadsheet_body(definition),
                fields="spreadsheetId",
            )
            .execute()
        )
        spreadsheet_id = response.get("spreadsheetId")
        if not isinstance(spreadsheet_id, str) or not spreadsheet_id:
            raise RuntimeError(
                "Google Sheets did not return a spreadsheet identifier."
            )
        return spreadsheet_id

    def find_messages(
        self,
        after_checkpoint: str | None,
    ) -> MailboxScan:
        service = self._build_google_service("gmail", "v1")
        query = "in:anywhere -in:spam -in:trash"
        if after_checkpoint is not None and after_checkpoint != "0":
            checkpoint_seconds = max(
                0,
                int(after_checkpoint) // 1000 - 1,
            )
            query = f"{query} after:{checkpoint_seconds}"

        message_ids: list[str] = []
        page_token: str | None = None
        while True:
            request_arguments: dict[str, Any] = {
                "userId": "me",
                "q": query,
                "maxResults": 500,
            }
            if page_token is not None:
                request_arguments["pageToken"] = page_token
            response = (
                service.users()
                .messages()
                .list(**request_arguments)
                .execute()
            )
            for reference in response.get("messages", []):
                message_id = reference.get("id")
                if isinstance(message_id, str) and message_id:
                    message_ids.append(message_id)
            next_page_token = response.get("nextPageToken")
            if not isinstance(next_page_token, str):
                break
            page_token = next_page_token

        messages = tuple(
            self._read_message(service, message_id)
            for message_id in message_ids
        )
        ordered_messages = tuple(
            sorted(messages, key=lambda message: message.timestamp)
        )
        checkpoint_candidates = [after_checkpoint or "0"]
        checkpoint_candidates.extend(
            self._timestamp_to_milliseconds(message.timestamp)
            for message in ordered_messages
        )
        checkpoint = max(checkpoint_candidates, key=int)
        return MailboxScan(
            messages=ordered_messages,
            checkpoint=checkpoint,
        )

    def append_application(
        self,
        spreadsheet_id: str,
        application: Application,
    ) -> None:
        self._append_row(
            spreadsheet_id,
            "Applications!A:E",
            self._application_row(application),
        )

    def count_matching_applications(
        self,
        spreadsheet_id: str,
        application: Application,
    ) -> int:
        return self._count_matching_rows(
            spreadsheet_id,
            "Applications!A2:E",
            self._application_row(application),
        )

    def append_needs_review(
        self,
        spreadsheet_id: str,
        review: NeedsReview,
    ) -> None:
        self._append_row(
            spreadsheet_id,
            "Needs Review!A:E",
            self._needs_review_row(review),
        )

    def count_matching_needs_review(
        self,
        spreadsheet_id: str,
        review: NeedsReview,
    ) -> int:
        return self._count_matching_rows(
            spreadsheet_id,
            "Needs Review!A2:E",
            self._needs_review_row(review),
        )

    def _append_row(
        self,
        spreadsheet_id: str,
        cell_range: str,
        row: list[str],
    ) -> None:
        service = self._build_google_service("sheets", "v4")
        (
            service.spreadsheets()
            .values()
            .append(
                spreadsheetId=spreadsheet_id,
                range=cell_range,
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": [row]},
            )
            .execute()
        )

    def _count_matching_rows(
        self,
        spreadsheet_id: str,
        cell_range: str,
        expected_row: list[str],
    ) -> int:
        service = self._build_google_service("sheets", "v4")
        response = (
            service.spreadsheets()
            .values()
            .get(
                spreadsheetId=spreadsheet_id,
                range=cell_range,
            )
            .execute()
        )
        return sum(
            list(row) + [""] * (5 - len(row)) == expected_row
            for row in response.get("values", [])
            if isinstance(row, list) and len(row) <= 5
        )

    @staticmethod
    def _application_row(application: Application) -> list[str]:
        return [
            application.company,
            application.position,
            application.application_date,
            application.status,
            application.stage,
        ]

    @staticmethod
    def _needs_review_row(review: NeedsReview) -> list[str]:
        return [
            review.email_date,
            review.sender,
            review.subject,
            review.gmail_link,
            review.reason,
        ]

    def _build_google_service(
        self,
        api_name: str,
        api_version: str,
    ) -> Any:
        if self._service_factory is not None:
            return self._service_factory(api_name, api_version)

        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from google_auth_oauthlib.flow import (  # type: ignore[import-untyped]
                InstalledAppFlow,
            )
            from googleapiclient.discovery import (  # type: ignore[import-untyped]
                build,
            )
        except ImportError as error:
            raise RuntimeError(
                "Google client libraries are not installed."
            ) from error

        credentials = None
        if self._config.token_path.exists():
            credentials = Credentials.from_authorized_user_file(  # type: ignore[no-untyped-call]
                str(self._config.token_path),
                self._config.scopes,
            )

        if credentials is None or not credentials.valid:
            if (
                credentials is not None
                and credentials.expired
                and credentials.refresh_token
            ):
                credentials.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(self._config.client_secrets_path),
                    self._config.scopes,
                )
                credentials = flow.run_local_server(port=0)
            self._cache_credentials(credentials.to_json())

        return build(
            api_name,
            api_version,
            credentials=credentials,
            cache_discovery=False,
        )

    @classmethod
    def _read_message(
        cls,
        service: Any,
        message_id: str,
    ) -> GmailMessage:
        response = (
            service.users()
            .messages()
            .get(
                userId="me",
                id=message_id,
                format="full",
            )
            .execute()
        )
        internal_date = response.get("internalDate")
        if not isinstance(internal_date, str):
            raise RuntimeError(
                "Gmail message did not include an internal timestamp."
            )
        payload = response.get("payload")
        if not isinstance(payload, dict):
            raise RuntimeError("Gmail message did not include a MIME payload.")
        headers = {
            str(header.get("name", "")).lower(): str(
                header.get("value", "")
            )
            for header in payload.get("headers", [])
            if isinstance(header, dict)
        }
        plain_parts, html_parts = cls._body_parts(payload)
        body = "\n".join(plain_parts)
        if not body and html_parts:
            parser = _PlainTextHtmlParser()
            for html_part in html_parts:
                parser.feed(html_part)
            body = parser.text

        timestamp = datetime.fromtimestamp(
            int(internal_date) / 1000,
            tz=timezone.utc,
        ).isoformat().replace("+00:00", "Z")
        return GmailMessage(
            message_id=message_id,
            sender=headers.get("from", ""),
            subject=headers.get("subject", ""),
            timestamp=timestamp,
            normalized_body=cls._normalize_body(body),
        )

    @classmethod
    def _body_parts(
        cls,
        payload: dict[str, Any],
    ) -> tuple[list[str], list[str]]:
        if cls._is_attachment(payload):
            return [], []

        plain_parts: list[str] = []
        html_parts: list[str] = []
        mime_type = payload.get("mimeType")
        body = payload.get("body")
        encoded_data = (
            body.get("data")
            if isinstance(body, dict)
            else None
        )
        if (
            isinstance(encoded_data, str)
            and mime_type in {"text/plain", "text/html"}
        ):
            decoded = cls._decode_body(encoded_data)
            if mime_type == "text/plain":
                plain_parts.append(decoded)
            elif mime_type == "text/html":
                html_parts.append(decoded)

        for part in payload.get("parts", []):
            if not isinstance(part, dict):
                continue
            child_plain, child_html = cls._body_parts(part)
            plain_parts.extend(child_plain)
            html_parts.extend(child_html)
        return plain_parts, html_parts

    @staticmethod
    def _is_attachment(payload: dict[str, Any]) -> bool:
        if payload.get("filename") or payload.get("mimeType") == (
            "message/rfc822"
        ):
            return True
        for header in payload.get("headers", []):
            if not isinstance(header, dict):
                continue
            name = str(header.get("name", "")).lower()
            value = str(header.get("value", "")).lower()
            if (
                name == "content-disposition"
                and value.split(";", 1)[0].strip() == "attachment"
            ):
                return True
        return False

    @staticmethod
    def _decode_body(encoded_data: str) -> str:
        padded_data = encoded_data + "=" * (-len(encoded_data) % 4)
        return base64.urlsafe_b64decode(padded_data).decode(
            "utf-8",
            errors="replace",
        )

    @staticmethod
    def _normalize_body(body: str) -> str:
        normalized_lines = (
            re.sub(r"[ \t]+", " ", line).strip()
            for line in body.replace("\r\n", "\n").replace("\r", "\n").split(
                "\n"
            )
        )
        return "\n".join(line for line in normalized_lines if line)

    @staticmethod
    def _timestamp_to_milliseconds(timestamp: str) -> str:
        parsed_timestamp = datetime.fromisoformat(
            timestamp.replace("Z", "+00:00")
        )
        return str(int(parsed_timestamp.timestamp() * 1000))

    def _cache_credentials(self, serialized_credentials: str) -> None:
        self._config.token_path.parent.mkdir(parents=True, exist_ok=True)
        self._config.token_path.write_text(
            serialized_credentials,
            encoding="utf-8",
        )
        self._config.token_path.chmod(0o600)

    @staticmethod
    def _spreadsheet_body(
        definition: TrackerSpreadsheet,
    ) -> dict[str, Any]:
        application_header = {
            "values": [
                {"userEnteredValue": {"stringValue": column}}
                for column in definition.applications_columns
            ]
        }
        sheets: list[dict[str, Any]] = [
            {
                "properties": {"sheetId": 0, "title": "Applications"},
                "data": [{"rowData": [application_header]}],
                "conditionalFormats": _application_status_formats(),
            }
        ]
        sheets.extend(
            _sheet_for_tab(tab)
            for tab in definition.additional_tabs
        )
        return {
            "properties": {"title": definition.title},
            "sheets": sheets,
        }


def _sheet_for_tab(tab: str) -> dict[str, Any]:
    if tab == "Needs Review":
        return {
            "properties": {"title": tab},
            "data": [
                {
                    "rowData": [
                        _header_row(
                            "Email Date",
                            "Sender",
                            "Subject",
                            "Gmail Link",
                            "Reason",
                        )
                    ]
                }
            ],
        }
    if tab != "Stats":
        return {"properties": {"title": tab}}
    return {
        "properties": {"title": tab},
        "data": [
            {
                "rowData": [
                    _stats_row("Metric", "Count"),
                    _stats_row(
                        "Total Applications",
                        "=COUNTA(Applications!A2:A)",
                    ),
                    _stats_row(
                        "Active",
                        '=COUNTIF(Applications!D2:D,"Active")',
                    ),
                    _stats_row(
                        "Rejected",
                        '=COUNTIF(Applications!D2:D,"Rejected")',
                    ),
                    _stats_row(
                        "Offers",
                        '=COUNTIF(Applications!D2:D,"Offer")',
                    ),
                    _stats_row(
                        "Withdrawn",
                        '=COUNTIF(Applications!D2:D,"Withdrawn")',
                    ),
                ]
            }
        ],
    }


def _stats_row(label: str, value: str) -> dict[str, Any]:
    value_type = "formulaValue" if value.startswith("=") else "stringValue"
    return {
        "values": [
            {"userEnteredValue": {"stringValue": label}},
            {"userEnteredValue": {value_type: value}},
        ]
    }


def _header_row(*columns: str) -> dict[str, Any]:
    return {
        "values": [
            {"userEnteredValue": {"stringValue": column}}
            for column in columns
        ]
    }


def _application_status_formats() -> list[dict[str, Any]]:
    return [
        _status_format("Active", red=0.85, green=1.0, blue=0.85),
        _status_format("Offer", red=0.85, green=0.92, blue=1.0),
        _status_format("Rejected", red=1.0, green=0.85, blue=0.85),
        _status_format("Withdrawn", red=0.9, green=0.9, blue=0.9),
    ]


def _status_format(
    status: str,
    *,
    red: float,
    green: float,
    blue: float,
) -> dict[str, Any]:
    return {
        "ranges": [
            {
                "sheetId": 0,
                "startRowIndex": 1,
                "startColumnIndex": 0,
                "endColumnIndex": 5,
            }
        ],
        "booleanRule": {
            "condition": {
                "type": "CUSTOM_FORMULA",
                "values": [{"userEnteredValue": f'=$D2="{status}"'}],
            },
            "format": {
                "backgroundColor": {
                    "red": red,
                    "green": green,
                    "blue": blue,
                }
            },
        },
    }


class _PlainTextHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._text_parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self._text_parts.append(data)

    @property
    def text(self) -> str:
        return " ".join(self._text_parts)
