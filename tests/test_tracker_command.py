from __future__ import annotations

from dataclasses import dataclass
from io import StringIO
from pathlib import Path

from job_tracker_email.command import main, run
from job_tracker_email.google_workspace import (
    GMAIL_READONLY_SCOPE,
    SHEETS_SCOPE,
    GoogleAuthConfig,
    TrackerSpreadsheet,
)
from job_tracker_email.state import SqliteTrackerState


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
            scopes=(GMAIL_READONLY_SCOPE, SHEETS_SCOPE),
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
        "Could not access Google Workspace. "
        "Check your OAuth setup and try again.\n"
    )
    assert "do-not-print-this" not in stderr.getvalue()
