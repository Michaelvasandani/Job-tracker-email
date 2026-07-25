from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import date
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
    ApplicationStatus,
    ClassificationInput,
    GmailMessage,
    MailboxScan,
    PendingApplicationWrite,
    PendingStatusUpdate,
    NeedsReview,
    ReviewProposal,
    StatusUpdate,
    ThreadApplication,
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

    def get_pending_status_update(
        self,
        message_id: str,
    ) -> PendingStatusUpdate | None: ...

    def record_pending_application_write(
        self,
        message_id: str,
        application: Application,
        matching_rows_before_write: int,
    ) -> None: ...

    def record_pending_status_update(
        self,
        message_id: str,
        row_number: int,
        status: ApplicationStatus,
    ) -> None: ...

    def get_application_for_thread(
        self,
        thread_id: str,
    ) -> ThreadApplication | None: ...

    def record_application_thread(
        self,
        thread_id: str,
        row_number: int,
        application: Application,
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
        start_date: date | None = None,
    ) -> MailboxScan: ...


class ApplicationClassifier(Protocol):
    def classify(
        self,
        message: ClassificationInput,
    ) -> Application | ReviewProposal | StatusUpdate | None: ...


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
    ) -> int: ...

    def list_applications(
        self,
        spreadsheet_id: str,
    ) -> tuple[Application | None, ...]: ...

    def update_application_status(
        self,
        spreadsheet_id: str,
        row_number: int,
        status: ApplicationStatus,
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


@dataclass(frozen=True)
class StatusUpdateProposal:
    message_id: str
    row_number: int
    status: ApplicationStatus


def run(
    *,
    workspace: GoogleWorkspace,
    state: TrackerState,
    stdout: TextIO,
    stderr: TextIO | None = None,
    sync: SyncAdapters | None = None,
    start_date: date | None = None,
    allow_large_import: bool = False,
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

    checkpoint = state.get_successful_checkpoint()
    scan = sync.mailbox.find_messages(
        checkpoint,
        start_date=start_date if checkpoint is None else None,
    )
    messages = tuple(
        sorted(
            (
                message
                for message in scan.messages
                if not state.has_processed_message(message.message_id)
            ),
            key=lambda message: message.timestamp,
        )
    )
    if len(messages) > 500 and not allow_large_import:
        error_output.write(
            f"Found {len(messages)} unprocessed Gmail messages. Re-run with "
            "--allow-large-import to classify this batch.\n"
        )
        return 1
    existing_applications = (
        sync.application_sheet.list_applications(spreadsheet_id)
        if spreadsheet_id is not None
        else ()
    )
    applications_by_row = {
        row_number: application
        for row_number, application in enumerate(existing_applications, start=2)
        if application is not None
    }
    planned_statuses: dict[int, ApplicationStatus] = {}
    application_proposals: list[tuple[str, Application]] = []
    status_update_proposals: list[StatusUpdateProposal] = []
    review_proposals: list[tuple[str, NeedsReview]] = []
    message_threads = {
        message.message_id: message.thread_id for message in messages
    }
    for message in messages:
        pending_write = state.get_pending_application_write(
            message.message_id
        )
        pending_status_update = state.get_pending_status_update(
            message.message_id
        )
        if pending_status_update is not None:
            status_update_proposals.append(
                StatusUpdateProposal(
                    message_id=message.message_id,
                    row_number=pending_status_update.row_number,
                    status=pending_status_update.status,
                )
            )
            planned_statuses[pending_status_update.row_number] = (
                pending_status_update.status
            )
            continue
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
        elif isinstance(outcome, StatusUpdate):
            thread_application = (
                state.get_application_for_thread(message.thread_id)
                if message.thread_id
                else None
            )
            if thread_application is not None:
                mapped_application = applications_by_row.get(
                    thread_application.row_number
                )
                if (
                    mapped_application is None
                    or mapped_application.company != thread_application.company
                    or mapped_application.position != thread_application.position
                    or mapped_application.application_date
                    != thread_application.application_date
                ):
                    review_proposals.append(
                        (
                            message.message_id,
                            _needs_review(
                                message,
                                "The Gmail thread no longer maps to its "
                                "original Application.",
                            ),
                        )
                    )
                    continue
                matching_rows = [thread_application.row_number]
            else:
                matching_rows = [
                    row_number
                    for row_number, application in applications_by_row.items()
                    if application.company == outcome.company
                    and application.position == outcome.position
                ]
            matching_proposals = [
                proposal_index
                for proposal_index, (_, application) in enumerate(
                    application_proposals
                )
                if application.company == outcome.company
                and application.position == outcome.position
            ]
            total_matches = len(matching_rows) + len(matching_proposals)
            if total_matches == 0:
                review_proposals.append(
                    (
                        message.message_id,
                        _needs_review(
                            message,
                            "No Application matches this Status update.",
                        ),
                    )
                )
            elif total_matches > 1:
                review_proposals.append(
                    (
                        message.message_id,
                        _needs_review(
                            message,
                            "Multiple Applications match this Status update.",
                        ),
                    )
                )
            elif matching_proposals:
                proposal_index = matching_proposals[0]
                proposal_message_id, application = application_proposals[
                    proposal_index
                ]
                if application.status == "Active":
                    application_proposals[proposal_index] = (
                        proposal_message_id,
                        replace(application, status=outcome.status),
                    )
                elif application.status != outcome.status:
                    review_proposals.append(
                        (
                            message.message_id,
                            _needs_review(
                                message,
                                _terminal_status_reason(
                                    application.status,
                                    outcome.status,
                                ),
                            ),
                        )
                    )
            else:
                row_number = matching_rows[0]
                current_status = planned_statuses.get(
                    row_number,
                    applications_by_row[row_number].status,
                )
                if current_status == outcome.status:
                    continue
                if current_status != "Active":
                    review_proposals.append(
                        (
                            message.message_id,
                            _needs_review(
                                message,
                                _terminal_status_reason(
                                    current_status,
                                    outcome.status,
                                ),
                            ),
                        )
                    )
                elif outcome.status != "Active":
                    status_update_proposals.append(
                        StatusUpdateProposal(
                            message_id=message.message_id,
                            row_number=row_number,
                            status=outcome.status,
                        )
                    )
                    planned_statuses[row_number] = outcome.status
        elif isinstance(outcome, ReviewProposal):
            review_proposals.append(
                (
                    message.message_id,
                    _needs_review(message, outcome.reason),
                )
            )

    if (
        not application_proposals
        and not status_update_proposals
        and not review_proposals
    ):
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
    if status_update_proposals:
        stdout.write("Proposed Status updates:\n")
        for proposal in status_update_proposals:
            application = applications_by_row[proposal.row_number]
            stdout.write(
                f"  Company: {application.company}\n"
                f"  Position: {application.position}\n"
                f"  Status: {proposal.status}\n"
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
                matching_rows_before_write = (
                    sync.application_sheet.count_matching_applications(
                        spreadsheet_id,
                        application,
                    )
                )
                state.record_pending_application_write(
                    message_id,
                    application,
                    matching_rows_before_write,
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
                row_number = sync.application_sheet.append_application(
                    spreadsheet_id,
                    application,
                )
                state.record_application_thread(
                    message_threads[message_id],
                    row_number,
                    application,
                )
        for proposal in status_update_proposals:
            pending_status_update = state.get_pending_status_update(
                proposal.message_id
            )
            if pending_status_update is None:
                state.record_pending_status_update(
                    proposal.message_id,
                    proposal.row_number,
                    proposal.status,
                )
            current_application = sync.application_sheet.list_applications(
                spreadsheet_id
            )[proposal.row_number - 2]
            if current_application is None:
                raise RuntimeError("The Application row no longer has a Status.")
            if current_application.status != proposal.status:
                sync.application_sheet.update_application_status(
                    spreadsheet_id,
                    proposal.row_number,
                    proposal.status,
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


def _needs_review(message: GmailMessage, reason: str) -> NeedsReview:
    return NeedsReview(
        email_date=message.timestamp[:10],
        sender=message.sender,
        subject=message.subject,
        gmail_link=(
            "https://mail.google.com/mail/u/0/#all/" f"{message.message_id}"
        ),
        reason=reason,
    )


def _terminal_status_reason(
    current_status: ApplicationStatus,
    proposed_status: ApplicationStatus,
) -> str:
    return (
        f"Cannot replace terminal {current_status} Status with "
        f"{proposed_status}."
    )


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
                start_date=arguments.start_date,
                allow_large_import=arguments.allow_large_import,
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
    parser.add_argument(
        "--start-date",
        type=_parse_start_date,
        help=(
            "earliest date to include in the initial Gmail history scan "
            "(YYYY-MM-DD)"
        ),
    )
    parser.add_argument(
        "--allow-large-import",
        action="store_true",
        help="classify a batch larger than 500 messages",
    )
    return parser


def _default_data_dir() -> Path:
    configured_path = os.environ.get("JOB_TRACKER_DATA_DIR")
    if configured_path:
        return Path(configured_path)
    return Path.home() / ".local" / "share" / "job-tracker-email"


def _parse_start_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "must be a calendar date in YYYY-MM-DD format"
        ) from error
