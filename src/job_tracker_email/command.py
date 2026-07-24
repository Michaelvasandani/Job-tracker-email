from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol, TextIO

from job_tracker_email.google_workspace import (
    GoogleApiWorkspace,
    GoogleAuthConfig,
    TrackerSpreadsheet,
)
from job_tracker_email.state import SqliteTrackerState


TRACKER_SPREADSHEET = TrackerSpreadsheet(
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


class GoogleWorkspace(Protocol):
    def create_spreadsheet(
        self, definition: TrackerSpreadsheet
    ) -> str: ...


class TrackerState(Protocol):
    def get_spreadsheet_id(self) -> str | None: ...

    def save_spreadsheet_id(self, spreadsheet_id: str) -> None: ...


def run(
    *,
    workspace: GoogleWorkspace,
    state: TrackerState,
    stdout: TextIO,
) -> int:
    if state.get_spreadsheet_id() is not None:
        stdout.write("Reusing Job Application Tracker.\n")
        return 0

    spreadsheet_id = workspace.create_spreadsheet(TRACKER_SPREADSHEET)
    state.save_spreadsheet_id(spreadsheet_id)
    stdout.write("Created Job Application Tracker.\n")
    return 0


WorkspaceFactory = Callable[[GoogleAuthConfig], GoogleWorkspace]


def main(
    argv: Sequence[str] | None = None,
    *,
    workspace_factory: WorkspaceFactory = GoogleApiWorkspace,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    output = sys.stdout if stdout is None else stdout
    error_output = sys.stderr if stderr is None else stderr
    arguments = _parser().parse_args(argv)
    data_dir = arguments.data_dir
    client_secrets_path = arguments.client_secrets
    if client_secrets_path is None:
        configured_path = os.environ.get(
            "JOB_TRACKER_GOOGLE_CLIENT_SECRETS"
        )
        client_secrets_path = (
            Path(configured_path)
            if configured_path
            else data_dir / "credentials.json"
        )

    auth_config = GoogleAuthConfig(
        client_secrets_path=client_secrets_path,
        token_path=data_dir / "google-token.json",
    )
    try:
        workspace = workspace_factory(auth_config)
        with SqliteTrackerState(data_dir / "tracker.sqlite3") as state:
            return run(workspace=workspace, state=state, stdout=output)
    except Exception:
        error_output.write(
            "Could not access Google Workspace. "
            "Check your OAuth setup and try again.\n"
        )
        return 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="job-tracker-email",
        description=(
            "Manually synchronize Gmail job Applications to Google Sheets."
        ),
    )
    parser.add_argument(
        "--client-secrets",
        type=Path,
        help=(
            "path to Google OAuth desktop client credentials "
            "(default: $JOB_TRACKER_GOOGLE_CLIENT_SECRETS or "
            "<data-dir>/credentials.json)"
        ),
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=_default_data_dir(),
        help=(
            "directory for cached authorization and local state "
            "(default: $JOB_TRACKER_DATA_DIR or "
            "~/.local/share/job-tracker-email)"
        ),
    )
    return parser


def _default_data_dir() -> Path:
    configured_path = os.environ.get("JOB_TRACKER_DATA_DIR")
    if configured_path:
        return Path(configured_path)
    return Path.home() / ".local" / "share" / "job-tracker-email"
