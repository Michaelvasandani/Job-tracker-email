from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TextIO

from job_tracker_email.google_workspace import (
    GoogleApiWorkspace,
    GoogleAuthConfig,
    TrackerSpreadsheet,
)
from job_tracker_email.openai_classifier import (
    OpenAIApplicationClassifier,
)
from job_tracker_email.state import SqliteTrackerState
from job_tracker_email.sync import (
    Application,
    ClassificationInput,
    MailboxScan,
    PendingApplicationWrite,
    NeedsReview,
    ReviewProposal,
)


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

    def get_successful_checkpoint(self) -> str | None: ...

    def has_processed_message(self, message_id: str) -> bool: ...

    def get_pending_application_write(
        self,
        message_id: str,
    ) -> PendingApplicationWrite | None: ...

    def record_pending_application_write(
        self,
        message_id: str,
        application: Application,
        matching_rows_before_write: int,
    ) -> None: ...

    def record_successful_sync(
        self,
        checkpoint: str,
        message_ids: tuple[str, ...],
    ) -> None: ...


class Mailbox(Protocol):
    def find_messages(
        self,
        after_checkpoint: str | None,
    ) -> MailboxScan: ...


class ApplicationClassifier(Protocol):
    def classify(
        self,
        message: ClassificationInput,
    ) -> Application | ReviewProposal | None: ...


class ApplicationSheet(Protocol):
    def count_matching_applications(
        self,
        spreadsheet_id: str,
        application: Application,
    ) -> int: ...

    def append_application(
        self,
        spreadsheet_id: str,
        application: Application,
    ) -> None: ...

    def count_matching_needs_review(
        self,
        spreadsheet_id: str,
        review: NeedsReview,
    ) -> int: ...

    def append_needs_review(
        self,
        spreadsheet_id: str,
        review: NeedsReview,
    ) -> None: ...


class GoogleSyncWorkspace(
    GoogleWorkspace,
    Mailbox,
    ApplicationSheet,
    Protocol,
):
    pass


@dataclass(frozen=True)
class SyncAdapters:
    mailbox: Mailbox
    classifier: ApplicationClassifier
    application_sheet: ApplicationSheet
    confirm: Callable[[], bool]


def run(
    *,
    workspace: GoogleWorkspace,
    state: TrackerState,
    stdout: TextIO,
    stderr: TextIO | None = None,
    sync: SyncAdapters | None = None,
) -> int:
    error_output = sys.stderr if stderr is None else stderr
    spreadsheet_id = state.get_spreadsheet_id()
    if sync is None:
        if spreadsheet_id is not None:
            stdout.write("Reusing Job Application Tracker.\n")
            return 0
        spreadsheet_id = workspace.create_spreadsheet(TRACKER_SPREADSHEET)
        state.save_spreadsheet_id(spreadsheet_id)
        stdout.write("Created Job Application Tracker.\n")
        return 0

    if spreadsheet_id is not None:
        stdout.write("Reusing Job Application Tracker.\n")

    scan = sync.mailbox.find_messages(state.get_successful_checkpoint())
    messages = tuple(
        message
        for message in scan.messages
        if not state.has_processed_message(message.message_id)
    )
    application_proposals: list[tuple[str, Application]] = []
    review_proposals: list[tuple[str, NeedsReview]] = []
    for message in messages:
        pending_write = state.get_pending_application_write(
            message.message_id
        )
        if pending_write is None:
            outcome = sync.classifier.classify(
                ClassificationInput(
                    sender=message.sender,
                    subject=message.subject,
                    timestamp=message.timestamp,
                    normalized_body=message.normalized_body,
                )
            )
        else:
            outcome = pending_write.application
        if isinstance(outcome, Application):
            application_proposals.append((message.message_id, outcome))
        elif isinstance(outcome, ReviewProposal):
            review_proposals.append(
                (
                    message.message_id,
                    NeedsReview(
                        email_date=message.timestamp[:10],
                        sender=message.sender,
                        subject=message.subject,
                        gmail_link=(
                            "https://mail.google.com/mail/u/0/#all/"
                            f"{message.message_id}"
                        ),
                        reason=outcome.reason,
                    ),
                )
            )

    if not application_proposals and not review_proposals:
        if spreadsheet_id is None:
            spreadsheet_id = workspace.create_spreadsheet(
                TRACKER_SPREADSHEET
            )
            state.save_spreadsheet_id(spreadsheet_id)
            stdout.write("Created Job Application Tracker.\n")
        state.record_successful_sync(
            scan.checkpoint,
            tuple(message.message_id for message in messages),
        )
        return 0

    if application_proposals:
        stdout.write("Proposed Applications:\n")
        for _, application in application_proposals:
            stdout.write(
                f"  Company: {application.company}\n"
                f"  Position: {application.position}\n"
                f"  Application Date: {application.application_date}\n"
                f"  Status: {application.status}\n"
                f"  Stage: {application.stage or '(blank)'}\n"
            )
    if review_proposals:
        stdout.write("Proposed Needs Review items:\n")
        for _, review in review_proposals:
            stdout.write(
                f"  Email Date: {review.email_date}\n"
                f"  Sender: {review.sender}\n"
                f"  Subject: {review.subject}\n"
                f"  Gmail Link: {review.gmail_link}\n"
                f"  Reason: {review.reason}\n"
            )

    if not sync.confirm():
        stdout.write("Cancelled; no Applications imported.\n")
        return 0

    if spreadsheet_id is None:
        spreadsheet_id = workspace.create_spreadsheet(
            TRACKER_SPREADSHEET
        )
        state.save_spreadsheet_id(spreadsheet_id)
        stdout.write("Created Job Application Tracker.\n")

    try:
        for message_id, application in application_proposals:
            pending_write = state.get_pending_application_write(
                message_id
            )
            if pending_write is None:
                matching_rows = (
                    sync.application_sheet.count_matching_applications(
                        spreadsheet_id,
                        application,
                    )
                )
                state.record_pending_application_write(
                    message_id,
                    application,
                    matching_rows,
                )
                should_append = True
            else:
                current_matching_rows = (
                    sync.application_sheet.count_matching_applications(
                        spreadsheet_id,
                        application,
                    )
                )
                should_append = (
                    current_matching_rows
                    <= pending_write.matching_rows_before_write
                )
            if should_append:
                sync.application_sheet.append_application(
                    spreadsheet_id,
                    application,
                )
        for _, review in review_proposals:
            if (
                sync.application_sheet.count_matching_needs_review(
                    spreadsheet_id,
                    review,
                )
                == 0
            ):
                sync.application_sheet.append_needs_review(
                    spreadsheet_id,
                    review,
                )
    except Exception:
        error_output.write(
            "Could not update Job Application Tracker. Try again.\n"
        )
        return 1
    state.record_successful_sync(
        scan.checkpoint,
        tuple(message.message_id for message in messages),
    )
    stdout.write("Application imported.\n")
    return 0


WorkspaceFactory = Callable[[GoogleAuthConfig], GoogleSyncWorkspace]
ClassifierFactory = Callable[[], ApplicationClassifier]


def main(
    argv: Sequence[str] | None = None,
    *,
    workspace_factory: WorkspaceFactory = GoogleApiWorkspace,
    classifier_factory: ClassifierFactory = OpenAIApplicationClassifier,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    input_stream = sys.stdin if stdin is None else stdin
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
            def confirm() -> bool:
                output.write(
                    "Import the proposed Application? [y/N] "
                )
                output.flush()
                return input_stream.readline().strip().lower() in {
                    "y",
                    "yes",
                }

            return run(
                workspace=workspace,
                state=state,
                stdout=output,
                stderr=error_output,
                sync=SyncAdapters(
                    mailbox=workspace,
                    classifier=classifier_factory(),
                    application_sheet=workspace,
                    confirm=confirm,
                ),
            )
    except Exception:
        error_output.write(
            "Could not synchronize Gmail with Job Application Tracker. "
            "Check OAuth and API configuration, then try again.\n"
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
