from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from io import StringIO
from pathlib import Path
from typing import Any

from job_tracker_email.command import main, run
from job_tracker_email.google_workspace import (
    DRIVE_FILE_SCOPE,
    GMAIL_READONLY_SCOPE,
    GoogleApiWorkspace,
    GoogleAuthConfig,
    TrackerSpreadsheet,
)
from job_tracker_email.state import SqliteTrackerState
from job_tracker_email.sync import (
    Application,
    ApplicationStatus,
    MailboxScan,
    NeedsReview,
    SheetStatusUpdate,
)


@dataclass
class CreatedSpreadsheet:
    spreadsheet_id: str
    definition: TrackerSpreadsheet


class FakeGoogleWorkspace:
    def __init__(self) -> None:
        self.created_spreadsheets: list[CreatedSpreadsheet] = []

    def create_spreadsheet(
        self, definition: TrackerSpreadsheet
    ) -> str:
        spreadsheet_id = f"spreadsheet-{len(self.created_spreadsheets) + 1}"
        self.created_spreadsheets.append(
            CreatedSpreadsheet(spreadsheet_id, definition)
        )
        return spreadsheet_id

    def find_messages(
        self,
        after_checkpoint: str | None,
        start_date: date | None = None,
    ) -> MailboxScan:
        return MailboxScan(messages=(), checkpoint=after_checkpoint or "0")

    def append_application(
        self,
        spreadsheet_id: str,
        application: Application,
    ) -> int:
        raise AssertionError("An empty mailbox cannot append an Application.")

    def count_matching_applications(
        self,
        spreadsheet_id: str,
        application: Application,
    ) -> int:
        return 0

    def list_applications(
        self,
        spreadsheet_id: str,
    ) -> tuple[Application, ...]:
        return ()

    def update_application_status(
        self,
        spreadsheet_id: str,
        row_number: int,
        status: ApplicationStatus,
    ) -> None:
        raise AssertionError("An empty mailbox cannot update an Application.")

    def apply_recovery_changes(
        self,
        spreadsheet_id: str,
        status_updates: tuple[SheetStatusUpdate, ...],
        reviews: tuple[NeedsReview, ...],
    ) -> None:
        raise AssertionError("An empty mailbox cannot update an Application.")

    def count_matching_needs_review(
        self,
        spreadsheet_id: str,
        review: NeedsReview,
    ) -> int:
        return 0

    def list_needs_review(
        self,
        spreadsheet_id: str,
    ) -> tuple[NeedsReview, ...]:
        return ()

    def append_needs_review(
        self,
        spreadsheet_id: str,
        review: NeedsReview,
    ) -> None:
        raise AssertionError("An empty mailbox cannot append Needs Review.")


class FakeGoogleRequest:
    def __init__(self, response: dict[str, Any]) -> None:
        self._response = response

    def execute(self) -> dict[str, Any]:
        return self._response


class RecordingSheetsService:
    def __init__(self) -> None:
        self.create_requests: list[dict[str, Any]] = []

    def spreadsheets(self) -> RecordingSheetsService:
        return self

    def create(self, **arguments: Any) -> FakeGoogleRequest:
        self.create_requests.append(arguments)
        return FakeGoogleRequest({"spreadsheetId": "spreadsheet-stats"})


def test_manual_command_creates_then_reuses_one_local_tracker(
    tmp_path: Path,
) -> None:
    workspace = FakeGoogleWorkspace()
    stdout = StringIO()
    state_path = tmp_path / "state.sqlite3"

    with SqliteTrackerState(state_path) as state:
        first_exit_code = run(
            workspace=workspace,
            state=state,
            stdout=stdout,
        )

    with SqliteTrackerState(state_path) as state:
        second_exit_code = run(
            workspace=workspace,
            state=state,
            stdout=stdout,
        )

    assert first_exit_code == 0
    assert second_exit_code == 0
    assert stdout.getvalue() == (
        "Created Job Application Tracker.\n"
        "Reusing Job Application Tracker.\n"
    )
    assert workspace.created_spreadsheets == [
        CreatedSpreadsheet(
            spreadsheet_id="spreadsheet-1",
            definition=TrackerSpreadsheet(
                title="Job Application Tracker",
                applications_columns=(
                    "Company",
                    "Position",
                    "Application Date",
                    "Status",
                    "Stage",
                ),
                additional_tabs=("Needs Review", "Stats"),
            ),
        )
    ]


def test_sheets_adapter_creates_formula_driven_stats_and_status_colors(
    tmp_path: Path,
) -> None:
    service = RecordingSheetsService()
    workspace = GoogleApiWorkspace(
        GoogleAuthConfig(
            client_secrets_path=tmp_path / "credentials.json",
            token_path=tmp_path / "token.json",
        ),
        service_factory=lambda api_name, api_version: service,
    )

    spreadsheet_id = workspace.create_spreadsheet(
        TrackerSpreadsheet(
            title="Job Application Tracker",
            applications_columns=(
                "Company",
                "Position",
                "Application Date",
                "Status",
                "Stage",
            ),
            additional_tabs=("Needs Review", "Stats"),
        )
    )

    assert spreadsheet_id == "spreadsheet-stats"
    sheet_definitions = service.create_requests[0]["body"]["sheets"]
    stats_sheet = next(
        sheet
        for sheet in sheet_definitions
        if sheet["properties"]["title"] == "Stats"
    )
    stats_rows = stats_sheet["data"][0]["rowData"]
    assert [
        [cell["userEnteredValue"] for cell in row["values"]]
        for row in stats_rows
    ] == [
        [{"stringValue": "Metric"}, {"stringValue": "Count"}],
        [
            {"stringValue": "Total Applications"},
            {"formulaValue": "=COUNTA(Applications!A2:A)"},
        ],
        [
            {"stringValue": "Active"},
            {"formulaValue": '=COUNTIF(Applications!D2:D,"Active")'},
        ],
        [
            {"stringValue": "Rejected"},
            {"formulaValue": '=COUNTIF(Applications!D2:D,"Rejected")'},
        ],
        [
            {"stringValue": "Offers"},
            {"formulaValue": '=COUNTIF(Applications!D2:D,"Offer")'},
        ],
        [
            {"stringValue": "Withdrawn"},
            {"formulaValue": '=COUNTIF(Applications!D2:D,"Withdrawn")'},
        ],
    ]
    applications_sheet = sheet_definitions[0]
    status_formats = applications_sheet["conditionalFormats"]
    assert [
        rule["booleanRule"]["condition"]["values"][0][
            "userEnteredValue"
        ]
        for rule in status_formats
    ] == [
        '=$D2="Active"',
        '=$D2="Offer"',
        '=$D2="Rejected"',
        '=$D2="Withdrawn"',
    ]
    assert [
        rule["booleanRule"]["format"]["backgroundColor"]
        for rule in status_formats
    ] == [
        {"red": 0.85, "green": 1.0, "blue": 0.85},
        {"red": 0.85, "green": 0.92, "blue": 1.0},
        {"red": 1.0, "green": 0.85, "blue": 0.85},
        {"red": 0.9, "green": 0.9, "blue": 0.9},
    ]
    assert [rule["ranges"] for rule in status_formats] == [
        [
            {
                "sheetId": 0,
                "startRowIndex": 1,
                "startColumnIndex": 0,
                "endColumnIndex": 5,
            }
        ]
    ] * 4
    review_sheet = next(
        sheet
        for sheet in sheet_definitions
        if sheet["properties"]["title"] == "Needs Review"
    )
    assert [
        cell["userEnteredValue"]["stringValue"]
        for cell in review_sheet["data"][0]["rowData"][0]["values"]
    ] == ["Email Date", "Sender", "Subject", "Gmail Link", "Reason"]


def test_installed_command_requests_only_required_google_permissions(
    tmp_path: Path,
) -> None:
    workspace = FakeGoogleWorkspace()
    received_configs: list[GoogleAuthConfig] = []
    stdout = StringIO()

    def create_workspace(config: GoogleAuthConfig) -> FakeGoogleWorkspace:
        received_configs.append(config)
        return workspace

    exit_code = main(
        [
            "--client-secrets",
            str(tmp_path / "credentials.json"),
            "--data-dir",
            str(tmp_path / "local-data"),
        ],
        workspace_factory=create_workspace,
        stdout=stdout,
    )

    assert exit_code == 0
    assert stdout.getvalue() == "Created Job Application Tracker.\n"
    assert received_configs == [
        GoogleAuthConfig(
            client_secrets_path=tmp_path / "credentials.json",
            token_path=tmp_path / "local-data" / "google-token.json",
            scopes=(GMAIL_READONLY_SCOPE, DRIVE_FILE_SCOPE),
        )
    ]
    assert (tmp_path / "local-data" / "tracker.sqlite3").exists()


def test_command_does_not_print_secrets_from_google_errors(
    tmp_path: Path,
) -> None:
    class FailingGoogleWorkspace:
        def create_spreadsheet(
            self, definition: TrackerSpreadsheet
        ) -> str:
            raise RuntimeError(
                "request failed: access_token=do-not-print-this"
            )

        def find_messages(
            self,
            after_checkpoint: str | None,
            start_date: date | None = None,
        ) -> MailboxScan:
            raise AssertionError("Spreadsheet creation must fail first.")

        def count_matching_applications(
            self,
            spreadsheet_id: str,
            application: Application,
        ) -> int:
            raise AssertionError("Spreadsheet creation must fail first.")

        def append_application(
            self,
            spreadsheet_id: str,
            application: Application,
        ) -> int:
            raise AssertionError("Spreadsheet creation must fail first.")

        def list_applications(
            self,
            spreadsheet_id: str,
        ) -> tuple[Application, ...]:
            raise AssertionError("Spreadsheet creation must fail first.")

        def update_application_status(
            self,
            spreadsheet_id: str,
            row_number: int,
            status: ApplicationStatus,
        ) -> None:
            raise AssertionError("Spreadsheet creation must fail first.")

        def apply_recovery_changes(
            self,
            spreadsheet_id: str,
            status_updates: tuple[SheetStatusUpdate, ...],
            reviews: tuple[NeedsReview, ...],
        ) -> None:
            raise AssertionError("Spreadsheet creation must fail first.")

        def count_matching_needs_review(
            self,
            spreadsheet_id: str,
            review: NeedsReview,
        ) -> int:
            raise AssertionError("Spreadsheet creation must fail first.")

        def list_needs_review(
            self,
            spreadsheet_id: str,
        ) -> tuple[NeedsReview, ...]:
            raise AssertionError("Spreadsheet creation must fail first.")

        def append_needs_review(
            self,
            spreadsheet_id: str,
            review: NeedsReview,
        ) -> None:
            raise AssertionError("Spreadsheet creation must fail first.")

    stdout = StringIO()
    stderr = StringIO()

    exit_code = main(
        ["--data-dir", str(tmp_path)],
        workspace_factory=lambda config: FailingGoogleWorkspace(),
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 1
    assert stdout.getvalue() == ""
    assert stderr.getvalue() == (
        "Could not synchronize Gmail with Job Application Tracker. "
        "Check OAuth and API configuration, then try again.\n"
    )
    assert "do-not-print-this" not in stderr.getvalue()
