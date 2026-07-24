from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"


@dataclass(frozen=True)
class GoogleAuthConfig:
    client_secrets_path: Path
    token_path: Path
    scopes: tuple[str, ...] = (GMAIL_READONLY_SCOPE, SHEETS_SCOPE)


@dataclass(frozen=True)
class TrackerSpreadsheet:
    title: str
    applications_columns: tuple[str, ...]
    additional_tabs: tuple[str, ...]


class GoogleApiWorkspace:
    """Google Sheets boundary backed by one installed-app OAuth grant."""

    def __init__(self, config: GoogleAuthConfig) -> None:
        self._config = config

    def create_spreadsheet(
        self, definition: TrackerSpreadsheet
    ) -> str:
        service = self._build_sheets_service()
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

    def _build_sheets_service(self) -> Any:
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
            "sheets",
            "v4",
            credentials=credentials,
            cache_discovery=False,
        )

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
                "properties": {"title": "Applications"},
                "data": [{"rowData": [application_header]}],
            }
        ]
        sheets.extend(
            {"properties": {"title": tab}}
            for tab in definition.additional_tabs
        )
        return {
            "properties": {"title": definition.title},
            "sheets": sheets,
        }
