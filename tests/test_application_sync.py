from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field, replace
from datetime import date
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from job_tracker_email.command import SyncAdapters, main, run
from job_tracker_email.google_workspace import (
    GoogleApiWorkspace,
    GoogleAuthConfig,
)
from job_tracker_email.openai_classifier import (
    OpenAIApplicationClassifier,
)
from job_tracker_email.sync import (
    Application,
    ApplicationStatus,
    ClassificationInput,
    GmailMessage,
    MailboxScan,
    NeedsReview,
    ReviewProposal,
    StatusUpdate,
    ThreadApplication,
)
from job_tracker_email.state import SqliteTrackerState


@dataclass
class FakeMailbox:
    scan: MailboxScan
    scan_requests: list[tuple[str | None, date | None]] = field(
        default_factory=list
    )

    def find_messages(
        self,
        after_checkpoint: str | None,
        start_date: date | None = None,
    ) -> MailboxScan:
        self.scan_requests.append((after_checkpoint, start_date))
        return self.scan


class RecordingClassifier:
    def __init__(
        self,
        result: Application | ReviewProposal | StatusUpdate | None,
    ) -> None:
        self.result = result
        self.received: list[ClassificationInput] = []

    def classify(
        self,
        message: ClassificationInput,
    ) -> Application | ReviewProposal | StatusUpdate | None:
        self.received.append(message)
        return self.result


class ScenarioClassifier:
    def __init__(
        self,
        outcomes: dict[str, Application | ReviewProposal | StatusUpdate | None],
    ) -> None:
        self._outcomes = outcomes
        self.received: list[ClassificationInput] = []

    def classify(
        self,
        message: ClassificationInput,
    ) -> Application | ReviewProposal | StatusUpdate | None:
        self.received.append(message)
        return self._outcomes[message.subject]


class RecordingApplicationSheet:
    def __init__(self) -> None:
        self.rows: list[tuple[str, Application]] = []
        self.review_rows: list[tuple[str, NeedsReview]] = []

    def append_application(
        self,
        spreadsheet_id: str,
        application: Application,
    ) -> int:
        self.rows.append((spreadsheet_id, application))
        return len(self.rows) + 1

    def count_matching_applications(
        self,
        spreadsheet_id: str,
        application: Application,
    ) -> int:
        return self.rows.count((spreadsheet_id, application))

    def append_needs_review(
        self,
        spreadsheet_id: str,
        review: NeedsReview,
    ) -> None:
        self.review_rows.append((spreadsheet_id, review))

    def count_matching_needs_review(
        self,
        spreadsheet_id: str,
        review: NeedsReview,
    ) -> int:
        return self.review_rows.count((spreadsheet_id, review))

    def list_applications(
        self,
        spreadsheet_id: str,
    ) -> tuple[Application | None, ...]:
        return tuple(
            application
            if application.status
            in {"Active", "Rejected", "Offer", "Withdrawn"}
            else None
            for row_spreadsheet_id, application in self.rows
            if row_spreadsheet_id == spreadsheet_id
        )

    def update_application_status(
        self,
        spreadsheet_id: str,
        row_number: int,
        status: ApplicationStatus,
    ) -> None:
        row_index = row_number - 2
        row_spreadsheet_id, application = self.rows[row_index]
        assert row_spreadsheet_id == spreadsheet_id
        self.rows[row_index] = (row_spreadsheet_id, replace(application, status=status))


class FailOnceApplicationSheet(RecordingApplicationSheet):
    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    def append_application(
        self,
        spreadsheet_id: str,
        application: Application,
    ) -> int:
        self.attempts += 1
        row_number = super().append_application(spreadsheet_id, application)
        if self.attempts == 1:
            raise RuntimeError(
                "write failed while handling private-email-body"
            )
        return row_number


class FailOnceStatusSheet(RecordingApplicationSheet):
    def __init__(self) -> None:
        super().__init__()
        self.status_attempts = 0

    def update_application_status(
        self,
        spreadsheet_id: str,
        row_number: int,
        status: ApplicationStatus,
    ) -> None:
        self.status_attempts += 1
        super().update_application_status(spreadsheet_id, row_number, status)
        if self.status_attempts == 1:
            raise RuntimeError("status write failed")


class ExistingTracker:
    def create_spreadsheet(self, definition: object) -> str:
        raise AssertionError("The existing tracker must be reused.")


class RecordingTracker:
    def __init__(self) -> None:
        self.created = False

    def create_spreadsheet(self, definition: object) -> str:
        self.created = True
        return "new-spreadsheet"


class FakeGoogleRequest:
    def __init__(self, response: dict[str, Any]) -> None:
        self._response = response

    def execute(self) -> dict[str, Any]:
        return self._response


class FakeGmailMessages:
    def __init__(self, message: dict[str, Any]) -> None:
        self._message = message

    def list(self, **arguments: Any) -> FakeGoogleRequest:
        return FakeGoogleRequest({"messages": [{"id": "gmail-private-1"}]})

    def get(self, **arguments: Any) -> FakeGoogleRequest:
        return FakeGoogleRequest(self._message)


class FakeGmailUsers:
    def __init__(self, message: dict[str, Any]) -> None:
        self._messages = FakeGmailMessages(message)

    def messages(self) -> FakeGmailMessages:
        return self._messages


class FakeGmailService:
    def __init__(self, message: dict[str, Any]) -> None:
        self._users = FakeGmailUsers(message)

    def users(self) -> FakeGmailUsers:
        return self._users


class RecordingGmailMessages:
    def __init__(self, messages: dict[str, dict[str, Any]]) -> None:
        self._messages = messages
        self.list_requests: list[dict[str, Any]] = []

    def list(self, **arguments: Any) -> FakeGoogleRequest:
        self.list_requests.append(arguments)
        return FakeGoogleRequest(
            {"messages": [{"id": message_id} for message_id in self._messages]}
        )

    def get(self, **arguments: Any) -> FakeGoogleRequest:
        return FakeGoogleRequest(self._messages[arguments["id"]])


class RecordingGmailService:
    def __init__(self, messages: dict[str, dict[str, Any]]) -> None:
        self._messages = RecordingGmailMessages(messages)

    def users(self) -> RecordingGmailService:
        return self

    def messages(self) -> RecordingGmailMessages:
        return self._messages


class FakeOpenAIResponses:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def create(self, **request: Any) -> SimpleNamespace:
        self.requests.append(request)
        return SimpleNamespace(
            output_text=json.dumps(
                {
                    "kind": "new_application",
                    "company": "Example Corp",
                    "position": "Software Engineer",
                    "application_date": "2026-07-24",
                }
            )
        )


class FakeOpenAIClient:
    def __init__(self) -> None:
        self.responses = FakeOpenAIResponses()


def encoded_body(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


class SyncingWorkspace(RecordingApplicationSheet):
    def __init__(self, scan: MailboxScan) -> None:
        super().__init__()
        self.scan = scan
        self.created = False
        self.scan_requests: list[tuple[str | None, date | None]] = []

    def create_spreadsheet(self, definition: object) -> str:
        self.created = True
        return "spreadsheet-from-main"

    def find_messages(
        self,
        after_checkpoint: str | None,
        start_date: date | None = None,
    ) -> MailboxScan:
        self.scan_requests.append((after_checkpoint, start_date))
        return self.scan


def test_approving_preview_appends_clear_application_and_checkpoints(
    tmp_path: Path,
) -> None:
    raw_body = (
        "Thank you for applying to Acme Corporation for the "
        "Senior Software Engineer - Infrastructure (REQ-481), Seattle role."
    )
    mailbox = FakeMailbox(
        MailboxScan(
            messages=(
                GmailMessage(
                    message_id="gmail-message-1",
                    sender="Acme Recruiting <notifications@greenhouse.io>",
                    subject="We received your application",
                    timestamp="2026-07-23T18:30:00Z",
                    normalized_body=raw_body,
                    thread_id="application-thread-1",
                ),
            ),
            checkpoint="checkpoint-101",
        )
    )
    application = Application(
        company="Acme Corporation",
        position="Senior Software Engineer",
        application_date="2026-07-23",
        status="Active",
        stage="",
    )
    classifier = RecordingClassifier(application)
    sheet = RecordingApplicationSheet()
    stdout = StringIO()
    state_path = tmp_path / "tracker.sqlite3"

    with SqliteTrackerState(state_path) as state:
        state.save_spreadsheet_id("spreadsheet-1")
        exit_code = run(
            workspace=ExistingTracker(),
            state=state,
            stdout=stdout,
            sync=SyncAdapters(
                mailbox=mailbox,
                classifier=classifier,
                application_sheet=sheet,
                confirm=lambda: True,
            ),
        )

        assert state.get_successful_checkpoint() == "checkpoint-101"
        assert state.has_processed_message("gmail-message-1")
        assert state.get_application_for_thread("application-thread-1") == (
            ThreadApplication(
                row_number=2,
                company="Acme Corporation",
                position="Senior Software Engineer",
                application_date="2026-07-23",
            )
        )

    assert exit_code == 0
    assert stdout.getvalue() == (
        "Reusing Job Application Tracker.\n"
        "Proposed Applications:\n"
        "  Company: Acme Corporation\n"
        "  Position: Senior Software Engineer\n"
        "  Application Date: 2026-07-23\n"
        "  Status: Active\n"
        "  Stage: (blank)\n"
        "Application imported.\n"
    )
    assert sheet.rows == [("spreadsheet-1", application)]
    assert classifier.received == [
        ClassificationInput(
            sender="Acme Recruiting <notifications@greenhouse.io>",
            subject="We received your application",
            timestamp="2026-07-23T18:30:00Z",
            normalized_body=raw_body,
        )
    ]


def test_failed_spreadsheet_write_can_retry_without_duplicate(
    tmp_path: Path,
) -> None:
    raw_body = "Your application for Staff Engineer has been submitted."
    mailbox = FakeMailbox(
        MailboxScan(
            messages=(
                GmailMessage(
                    message_id="gmail-message-2",
                    sender="Example Corp <recruiting@example.com>",
                    subject="Application received",
                    timestamp="2026-07-24T10:00:00Z",
                    normalized_body=raw_body,
                ),
            ),
            checkpoint="checkpoint-202",
        )
    )
    application = Application(
        company="Example Corp",
        position="Staff Engineer",
        application_date="2026-07-24",
    )
    classifier = RecordingClassifier(application)
    sheet = FailOnceApplicationSheet()
    stdout = StringIO()
    stderr = StringIO()
    state_path = tmp_path / "tracker.sqlite3"

    with SqliteTrackerState(state_path) as state:
        state.save_spreadsheet_id("spreadsheet-2")
        first_exit_code = run(
            workspace=ExistingTracker(),
            state=state,
            stdout=stdout,
            stderr=stderr,
            sync=SyncAdapters(
                mailbox=mailbox,
                classifier=classifier,
                application_sheet=sheet,
                confirm=lambda: True,
            ),
        )

        assert first_exit_code == 1
        assert state.get_successful_checkpoint() is None
        assert not state.has_processed_message("gmail-message-2")
        assert raw_body.encode() not in state_path.read_bytes()

        second_exit_code = run(
            workspace=ExistingTracker(),
            state=state,
            stdout=stdout,
            stderr=stderr,
            sync=SyncAdapters(
                mailbox=mailbox,
                classifier=classifier,
                application_sheet=sheet,
                confirm=lambda: True,
            ),
        )

        assert state.get_successful_checkpoint() == "checkpoint-202"
        assert state.has_processed_message("gmail-message-2")

    assert first_exit_code == 1
    assert second_exit_code == 0
    assert sheet.rows == [("spreadsheet-2", application)]
    assert len(classifier.received) == 1
    assert stderr.getvalue() == (
        "Could not update Job Application Tracker. Try again.\n"
    )
    assert raw_body not in stdout.getvalue()
    assert raw_body not in stderr.getvalue()


def test_rejecting_preview_changes_neither_sheet_nor_checkpoint(
    tmp_path: Path,
) -> None:
    raw_body = "We received your application for Product Designer."
    mailbox = FakeMailbox(
        MailboxScan(
            messages=(
                GmailMessage(
                    message_id="gmail-message-3",
                    sender="Design Co <jobs@design.example>",
                    subject="Application confirmation",
                    timestamp="2026-07-24T11:00:00Z",
                    normalized_body=raw_body,
                ),
            ),
            checkpoint="checkpoint-303",
        )
    )
    classifier = RecordingClassifier(
        Application(
            company="Design Co",
            position="Product Designer",
            application_date="2026-07-24",
        )
    )
    sheet = RecordingApplicationSheet()
    tracker = RecordingTracker()
    stdout = StringIO()
    state_path = tmp_path / "tracker.sqlite3"

    with SqliteTrackerState(state_path) as state:
        def reject_after_observing_preview() -> bool:
            assert "Proposed Applications:" in stdout.getvalue()
            assert sheet.rows == []
            assert not tracker.created
            assert state.get_spreadsheet_id() is None
            assert state.get_successful_checkpoint() is None
            return False

        exit_code = run(
            workspace=tracker,
            state=state,
            stdout=stdout,
            sync=SyncAdapters(
                mailbox=mailbox,
                classifier=classifier,
                application_sheet=sheet,
                confirm=reject_after_observing_preview,
            ),
        )

        assert state.get_successful_checkpoint() is None
        assert state.get_spreadsheet_id() is None
        assert not state.has_processed_message("gmail-message-3")

    assert exit_code == 0
    assert not tracker.created
    assert sheet.rows == []
    assert stdout.getvalue().endswith(
        "Cancelled; no Applications imported.\n"
    )
    assert raw_body not in stdout.getvalue()
    assert raw_body.encode() not in state_path.read_bytes()


def test_confirmed_review_proposal_records_gmail_context_without_body(
    tmp_path: Path,
) -> None:
    raw_body = "We would like to discuss your application for a role."
    mailbox = FakeMailbox(
        MailboxScan(
            messages=(
                GmailMessage(
                    message_id="uncertain-hiring-message",
                    sender="Hiring Team <hiring@example.com>",
                    subject="Let's talk about your application",
                    timestamp="2026-07-24T11:00:00Z",
                    normalized_body=raw_body,
                ),
            ),
            checkpoint="checkpoint-review",
        )
    )
    classifier = RecordingClassifier(
        ReviewProposal(
            reason="The application date cannot be established."
        )
    )
    sheet = RecordingApplicationSheet()
    stdout = StringIO()
    state_path = tmp_path / "tracker.sqlite3"

    with SqliteTrackerState(state_path) as state:
        state.save_spreadsheet_id("spreadsheet-review")

        def approve_after_observing_preview() -> bool:
            assert "Proposed Needs Review items:" in stdout.getvalue()
            assert raw_body not in stdout.getvalue()
            assert sheet.rows == []
            assert sheet.review_rows == []
            return True

        exit_code = run(
            workspace=ExistingTracker(),
            state=state,
            stdout=stdout,
            sync=SyncAdapters(
                mailbox=mailbox,
                classifier=classifier,
                application_sheet=sheet,
                confirm=approve_after_observing_preview,
            ),
        )

        assert exit_code == 0
        assert state.has_processed_message("uncertain-hiring-message")
        assert raw_body.encode() not in state_path.read_bytes()
        assert sheet.review_rows == [
            (
                "spreadsheet-review",
                NeedsReview(
                    email_date="2026-07-24",
                    sender="Hiring Team <hiring@example.com>",
                    subject="Let's talk about your application",
                    gmail_link=(
                        "https://mail.google.com/mail/u/0/#all/"
                        "uncertain-hiring-message"
                    ),
                    reason="The application date cannot be established.",
                ),
            )
        ]

        sheet.review_rows.clear()
        rerun_exit_code = run(
            workspace=ExistingTracker(),
            state=state,
            stdout=StringIO(),
            sync=SyncAdapters(
                mailbox=mailbox,
                classifier=classifier,
                application_sheet=sheet,
                confirm=lambda: True,
            ),
        )

    assert rerun_exit_code == 0
    assert sheet.rows == []
    assert sheet.review_rows == []
    assert raw_body not in stdout.getvalue()


def test_ignored_message_advances_checkpoint_without_a_proposal(
    tmp_path: Path,
) -> None:
    mailbox = FakeMailbox(
        MailboxScan(
            messages=(
                GmailMessage(
                    message_id="saved-job-reminder",
                    sender="Job Board <alerts@example.com>",
                    subject="A saved job is waiting",
                    timestamp="2026-07-24T12:00:00Z",
                    normalized_body="A job you saved is still available.",
                ),
            ),
            checkpoint="checkpoint-ignored",
        )
    )
    sheet = RecordingApplicationSheet()

    with SqliteTrackerState(tmp_path / "tracker.sqlite3") as state:
        state.save_spreadsheet_id("spreadsheet-ignored")
        exit_code = run(
            workspace=ExistingTracker(),
            state=state,
            stdout=StringIO(),
            sync=SyncAdapters(
                mailbox=mailbox,
                classifier=RecordingClassifier(None),
                application_sheet=sheet,
                confirm=lambda: (_ for _ in ()).throw(
                    AssertionError("Ignored mail needs no confirmation.")
                ),
            ),
        )

        assert state.get_successful_checkpoint() == "checkpoint-ignored"
        assert state.has_processed_message("saved-job-reminder")

    assert exit_code == 0
    assert sheet.rows == []
    assert sheet.review_rows == []


@pytest.mark.parametrize(
    ("message_id", "subject", "outcome", "requires_confirmation"),
    [
        pytest.param(
            "generic-job-alert",
            "New jobs for you",
            None,
            False,
            id="generic-job-alert-is-ignored",
        ),
        pytest.param(
            "recruiter-outreach",
            "Interested in a role?",
            None,
            False,
            id="pre-application-recruiter-outreach-is-ignored",
        ),
        pytest.param(
            "incomplete-application",
            "Finish your application",
            None,
            False,
            id="unsubmitted-application-is-ignored",
        ),
        pytest.param(
            "unknown-company",
            "We received your application",
            ReviewProposal("The prospective employer is unclear."),
            True,
            id="uncertain-company-is-reviewed",
        ),
        pytest.param(
            "unknown-position",
            "Interview invitation",
            ReviewProposal("The Position is unclear."),
            True,
            id="uncertain-position-is-reviewed",
        ),
        pytest.param(
            "unknown-submission-date",
            "Next interview steps",
            ReviewProposal("The Application Date is not established."),
            True,
            id="later-hiring-message-is-reviewed",
        ),
        pytest.param(
            "ambiguous-application",
            "Update on your application",
            ReviewProposal("The Application identity is ambiguous."),
            True,
            id="uncertain-application-identity-is-reviewed",
        ),
        pytest.param(
            "uncertain-status",
            "Good news about your application",
            ReviewProposal("The Status is uncertain."),
            True,
            id="uncertain-status-is-reviewed",
        ),
        pytest.param(
            "non-english-message",
            "Actualización de su solicitud",
            ReviewProposal("The language is unsupported."),
            True,
            id="unsupported-language-is-reviewed",
        ),
        pytest.param(
            "alternate-position",
            "Consider a different position",
            ReviewProposal("No submission evidence supports this Position."),
            True,
            id="alternate-position-is-reviewed",
        ),
    ],
)
def test_conservative_outcomes_never_automatically_create_an_application(
    tmp_path: Path,
    message_id: str,
    subject: str,
    outcome: ReviewProposal | None,
    requires_confirmation: bool,
) -> None:
    mailbox = FakeMailbox(
        MailboxScan(
            messages=(
                GmailMessage(
                    message_id=message_id,
                    sender="Mailbox Sender <sender@example.com>",
                    subject=subject,
                    timestamp="2026-07-24T13:00:00Z",
                    normalized_body="Representative mailbox scenario.",
                ),
            ),
            checkpoint=f"checkpoint-{message_id}",
        )
    )
    sheet = RecordingApplicationSheet()
    confirmations = 0

    def confirm() -> bool:
        nonlocal confirmations
        confirmations += 1
        return True

    with SqliteTrackerState(tmp_path / "tracker.sqlite3") as state:
        state.save_spreadsheet_id("spreadsheet-conservative-outcomes")
        exit_code = run(
            workspace=ExistingTracker(),
            state=state,
            stdout=StringIO(),
            sync=SyncAdapters(
                mailbox=mailbox,
                classifier=RecordingClassifier(outcome),
                application_sheet=sheet,
                confirm=confirm,
            ),
        )

        assert state.has_processed_message(message_id)

    assert exit_code == 0
    assert confirmations == int(requires_confirmation)
    assert sheet.rows == []
    assert len(sheet.review_rows) == int(requires_confirmation)


def test_installed_command_syncs_through_controllable_boundaries(
    tmp_path: Path,
) -> None:
    workspace = SyncingWorkspace(
        MailboxScan(
            messages=(
                GmailMessage(
                    message_id="gmail-message-main",
                    sender="Northstar <jobs@northstar.example>",
                    subject="Thanks for applying",
                    timestamp="2026-07-24T12:00:00Z",
                    normalized_body="We received your application.",
                ),
            ),
            checkpoint="checkpoint-main",
        )
    )
    application = Application(
        company="Northstar",
        position="Engineering Manager",
        application_date="2026-07-24",
    )
    classifier = RecordingClassifier(application)
    stdout = StringIO()

    exit_code = main(
        ["--data-dir", str(tmp_path)],
        workspace_factory=lambda config: workspace,
        classifier_factory=lambda: classifier,
        stdin=StringIO("yes\n"),
        stdout=stdout,
    )

    assert exit_code == 0
    assert workspace.created
    assert workspace.rows == [("spreadsheet-from-main", application)]
    assert stdout.getvalue().endswith(
        "  Stage: (blank)\n"
        "Import the proposed Application? [y/N] "
        "Created Job Application Tracker.\n"
        "Application imported.\n"
    )
    with SqliteTrackerState(tmp_path / "tracker.sqlite3") as state:
        assert state.get_successful_checkpoint() == "checkpoint-main"
        assert state.has_processed_message("gmail-message-main")


def test_distinct_submissions_with_same_fields_create_separate_applications(
    tmp_path: Path,
) -> None:
    mailbox = FakeMailbox(
        MailboxScan(
            messages=(
                GmailMessage(
                    message_id="submission-one",
                    sender="Example Corp <jobs@example.com>",
                    subject="Application received",
                    timestamp="2026-07-24T13:00:00Z",
                    normalized_body="First submission confirmation.",
                ),
                GmailMessage(
                    message_id="submission-two",
                    sender="Example Corp <jobs@example.com>",
                    subject="Application received",
                    timestamp="2026-07-24T14:00:00Z",
                    normalized_body="Second submission confirmation.",
                ),
            ),
            checkpoint="checkpoint-two-submissions",
        )
    )
    application = Application(
        company="Example Corp",
        position="Software Engineer",
        application_date="2026-07-24",
    )
    sheet = RecordingApplicationSheet()
    state_path = tmp_path / "tracker.sqlite3"

    with SqliteTrackerState(state_path) as state:
        state.save_spreadsheet_id("spreadsheet-two-submissions")
        exit_code = run(
            workspace=ExistingTracker(),
            state=state,
            stdout=StringIO(),
            sync=SyncAdapters(
                mailbox=mailbox,
                classifier=RecordingClassifier(application),
                application_sheet=sheet,
                confirm=lambda: True,
            ),
        )

    assert exit_code == 0
    assert sheet.rows == [
        ("spreadsheet-two-submissions", application),
        ("spreadsheet-two-submissions", application),
    ]


def test_incremental_updates_are_chronological_and_preserve_manual_fields(
    tmp_path: Path,
) -> None:
    sheet = RecordingApplicationSheet()
    sheet.rows = [
        (
            "spreadsheet-status",
            Application(
                company="Acme",
                position="Senior Engineer",
                application_date="2026-07-01",
                stage="Manually entered interview stage",
            ),
        )
    ]
    mailbox = FakeMailbox(
        MailboxScan(
            messages=(
                GmailMessage(
                    message_id="later-rejection",
                    sender="Acme <jobs@acme.example>",
                    subject="Rejection",
                    timestamp="2026-07-24T10:00:00Z",
                    normalized_body="The position was filled.",
                ),
                GmailMessage(
                    message_id="earlier-offer",
                    sender="Acme <jobs@acme.example>",
                    subject="Offer",
                    timestamp="2026-07-24T09:00:00Z",
                    normalized_body="We are pleased to offer you the role.",
                ),
            ),
            checkpoint="checkpoint-status",
        )
    )
    classifier = ScenarioClassifier(
        {
            "Offer": StatusUpdate(
                company="Acme",
                position="Senior Engineer",
                status="Offer",
            ),
            "Rejection": StatusUpdate(
                company="Acme",
                position="Senior Engineer",
                status="Rejected",
            ),
        }
    )

    with SqliteTrackerState(tmp_path / "tracker.sqlite3") as state:
        state.save_spreadsheet_id("spreadsheet-status")
        state.record_successful_sync("previous-checkpoint", ())
        exit_code = run(
            workspace=ExistingTracker(),
            state=state,
            stdout=StringIO(),
            sync=SyncAdapters(
                mailbox=mailbox,
                classifier=classifier,
                application_sheet=sheet,
                confirm=lambda: True,
            ),
        )

        assert state.get_successful_checkpoint() == "checkpoint-status"
        assert state.has_processed_message("earlier-offer")
        assert state.has_processed_message("later-rejection")

    assert exit_code == 0
    assert [item.subject for item in classifier.received] == [
        "Offer",
        "Rejection",
    ]
    assert sheet.rows == [
        (
            "spreadsheet-status",
            Application(
                company="Acme",
                position="Senior Engineer",
                application_date="2026-07-01",
                status="Offer",
                stage="Manually entered interview stage",
            ),
        )
    ]
    assert sheet.review_rows == [
        (
            "spreadsheet-status",
            NeedsReview(
                email_date="2026-07-24",
                sender="Acme <jobs@acme.example>",
                subject="Rejection",
                gmail_link=(
                    "https://mail.google.com/mail/u/0/#all/later-rejection"
                ),
                reason="Cannot replace terminal Offer Status with Rejected.",
            ),
        )
    ]


@pytest.mark.parametrize(
    ("update_status", "expected_status"),
    [
        pytest.param("Active", "Active", id="advancement-stays-active"),
        pytest.param("Rejected", "Rejected", id="explicit-rejection"),
        pytest.param("Offer", "Offer", id="explicit-offer"),
        pytest.param("Withdrawn", "Withdrawn", id="pre-offer-withdrawal"),
    ],
)
def test_unique_active_application_accepts_allowed_status_update(
    tmp_path: Path,
    update_status: str,
    expected_status: str,
) -> None:
    sheet = RecordingApplicationSheet()
    sheet.rows = [
        (
            "spreadsheet-transition",
            Application(
                company="Example Corp",
                position="Designer",
                application_date="2026-07-01",
            ),
        )
    ]
    message = GmailMessage(
        message_id=f"status-{update_status}",
        sender="Example Corp <jobs@example.com>",
        subject="Status update",
        timestamp="2026-07-24T12:00:00Z",
        normalized_body="A status update.",
    )

    with SqliteTrackerState(tmp_path / "tracker.sqlite3") as state:
        state.save_spreadsheet_id("spreadsheet-transition")
        exit_code = run(
            workspace=ExistingTracker(),
            state=state,
            stdout=StringIO(),
            sync=SyncAdapters(
                mailbox=FakeMailbox(
                    MailboxScan(
                        messages=(message,),
                        checkpoint="checkpoint-transition",
                    )
                ),
                classifier=RecordingClassifier(
                    StatusUpdate(
                        company="Example Corp",
                        position="Designer",
                        status=cast(ApplicationStatus, update_status),
                    )
                ),
                application_sheet=sheet,
                confirm=lambda: True,
            ),
        )

    assert exit_code == 0
    assert sheet.rows[0][1].status == expected_status
    assert sheet.review_rows == []


def test_ambiguous_status_update_is_reviewed_without_changing_any_row(
    tmp_path: Path,
) -> None:
    application = Application(
        company="Example Corp",
        position="Engineer",
        application_date="2026-07-01",
    )
    sheet = RecordingApplicationSheet()
    sheet.rows = [
        ("spreadsheet-ambiguous", application),
        ("spreadsheet-ambiguous", application),
    ]

    with SqliteTrackerState(tmp_path / "tracker.sqlite3") as state:
        state.save_spreadsheet_id("spreadsheet-ambiguous")
        exit_code = run(
            workspace=ExistingTracker(),
            state=state,
            stdout=StringIO(),
            sync=SyncAdapters(
                mailbox=FakeMailbox(
                    MailboxScan(
                        messages=(
                            GmailMessage(
                                message_id="ambiguous-update",
                                sender="Example Corp <jobs@example.com>",
                                subject="Update",
                                timestamp="2026-07-24T12:00:00Z",
                                normalized_body="The role has been filled.",
                            ),
                        ),
                        checkpoint="checkpoint-ambiguous",
                    )
                ),
                classifier=RecordingClassifier(
                    StatusUpdate(
                        company="Example Corp",
                        position="Engineer",
                        status="Rejected",
                    )
                ),
                application_sheet=sheet,
                confirm=lambda: True,
            ),
        )

    assert exit_code == 0
    assert sheet.rows == [
        ("spreadsheet-ambiguous", application),
        ("spreadsheet-ambiguous", application),
    ]
    assert sheet.review_rows[0][1].reason == (
        "Multiple Applications match this Status update."
    )


def test_gmail_thread_relationship_disambiguates_same_role_applications(
    tmp_path: Path,
) -> None:
    application = Application(
        company="Example Corp",
        position="Engineer",
        application_date="2026-07-01",
    )
    sheet = RecordingApplicationSheet()
    sheet.rows = [
        ("spreadsheet-thread", application),
        ("spreadsheet-thread", application),
    ]

    with SqliteTrackerState(tmp_path / "tracker.sqlite3") as state:
        state.save_spreadsheet_id("spreadsheet-thread")
        state.record_application_thread(
            "thread-second-application",
            3,
            application,
        )
        exit_code = run(
            workspace=ExistingTracker(),
            state=state,
            stdout=StringIO(),
            sync=SyncAdapters(
                mailbox=FakeMailbox(
                    MailboxScan(
                        messages=(
                            GmailMessage(
                                message_id="thread-status-update",
                                thread_id="thread-second-application",
                                sender="Example Corp <jobs@example.com>",
                                subject="Update",
                                timestamp="2026-07-24T12:00:00Z",
                                normalized_body="The role was filled.",
                            ),
                        ),
                        checkpoint="checkpoint-thread",
                    )
                ),
                classifier=RecordingClassifier(
                    StatusUpdate(
                        company="Example Corp",
                        position="Engineer",
                        status="Rejected",
                    )
                ),
                application_sheet=sheet,
                confirm=lambda: True,
            ),
        )

    assert exit_code == 0
    assert sheet.rows[0][1].status == "Active"
    assert sheet.rows[1][1].status == "Rejected"
    assert sheet.review_rows == []


def test_stale_gmail_thread_mapping_is_reviewed_without_changing_a_row(
    tmp_path: Path,
) -> None:
    original_application = Application(
        company="Example Corp",
        position="Engineer",
        application_date="2026-07-01",
    )
    replacement_application = Application(
        company="Example Corp",
        position="Engineer",
        application_date="2026-07-15",
    )
    sheet = RecordingApplicationSheet()
    sheet.rows = [("spreadsheet-stale-thread", replacement_application)]

    with SqliteTrackerState(tmp_path / "tracker.sqlite3") as state:
        state.save_spreadsheet_id("spreadsheet-stale-thread")
        state.record_application_thread(
            "old-thread",
            2,
            original_application,
        )
        exit_code = run(
            workspace=ExistingTracker(),
            state=state,
            stdout=StringIO(),
            sync=SyncAdapters(
                mailbox=FakeMailbox(
                    MailboxScan(
                        messages=(
                            GmailMessage(
                                message_id="stale-thread-update",
                                thread_id="old-thread",
                                sender="Example Corp <jobs@example.com>",
                                subject="Update",
                                timestamp="2026-07-24T12:00:00Z",
                                normalized_body="The role was filled.",
                            ),
                        ),
                        checkpoint="checkpoint-stale-thread",
                    )
                ),
                classifier=RecordingClassifier(
                    StatusUpdate(
                        company="Example Corp",
                        position="Engineer",
                        status="Rejected",
                    )
                ),
                application_sheet=sheet,
                confirm=lambda: True,
            ),
        )

    assert exit_code == 0
    assert sheet.rows == [("spreadsheet-stale-thread", replacement_application)]
    assert sheet.review_rows[0][1].reason == (
        "The Gmail thread no longer maps to its original Application."
    )


def test_status_update_uses_the_actual_sheet_row_after_an_invalid_row(
    tmp_path: Path,
) -> None:
    sheet = RecordingApplicationSheet()
    sheet.rows = [
        (
            "spreadsheet-row-address",
            Application(
                company="Manual row",
                position="Do not alter",
                application_date="2026-07-01",
                status=cast(ApplicationStatus, ""),
            ),
        ),
        (
            "spreadsheet-row-address",
            Application(
                company="Example Corp",
                position="Engineer",
                application_date="2026-07-02",
            ),
        ),
    ]

    with SqliteTrackerState(tmp_path / "tracker.sqlite3") as state:
        state.save_spreadsheet_id("spreadsheet-row-address")
        exit_code = run(
            workspace=ExistingTracker(),
            state=state,
            stdout=StringIO(),
            sync=SyncAdapters(
                mailbox=FakeMailbox(
                    MailboxScan(
                        messages=(
                            GmailMessage(
                                message_id="update-second-row",
                                sender="Example Corp <jobs@example.com>",
                                subject="Offer",
                                timestamp="2026-07-24T12:00:00Z",
                                normalized_body="An offer.",
                            ),
                        ),
                        checkpoint="checkpoint-row-address",
                    )
                ),
                classifier=RecordingClassifier(
                    StatusUpdate(
                        company="Example Corp",
                        position="Engineer",
                        status="Offer",
                    )
                ),
                application_sheet=sheet,
                confirm=lambda: True,
            ),
        )

    assert exit_code == 0
    assert str(sheet.rows[0][1].status) == ""
    assert sheet.rows[1][1].status == "Offer"


def test_failed_status_update_retries_without_reclassifying_or_rewriting(
    tmp_path: Path,
) -> None:
    sheet = FailOnceStatusSheet()
    sheet.rows = [
        (
            "spreadsheet-retry-status",
            Application(
                company="Example Corp",
                position="Engineer",
                application_date="2026-07-01",
            ),
        )
    ]
    message = GmailMessage(
        message_id="retry-status",
        sender="Example Corp <jobs@example.com>",
        subject="Offer",
        timestamp="2026-07-24T12:00:00Z",
        normalized_body="An offer.",
    )
    classifier = RecordingClassifier(
        StatusUpdate(
            company="Example Corp",
            position="Engineer",
            status="Offer",
        )
    )
    state_path = tmp_path / "tracker.sqlite3"

    with SqliteTrackerState(state_path) as state:
        state.save_spreadsheet_id("spreadsheet-retry-status")
        sync = SyncAdapters(
            mailbox=FakeMailbox(
                MailboxScan(messages=(message,), checkpoint="checkpoint-retry")
            ),
            classifier=classifier,
            application_sheet=sheet,
            confirm=lambda: True,
        )
        assert run(
            workspace=ExistingTracker(), state=state, stdout=StringIO(), sync=sync
        ) == 1
        assert state.get_successful_checkpoint() is None
        assert run(
            workspace=ExistingTracker(), state=state, stdout=StringIO(), sync=sync
        ) == 0
        assert state.get_successful_checkpoint() == "checkpoint-retry"

    assert sheet.rows[0][1].status == "Offer"
    assert sheet.status_attempts == 1
    assert len(classifier.received) == 1


@pytest.mark.parametrize(
    ("current_status", "proposed_status", "requires_review"),
    [
        pytest.param("Rejected", "Offer", True, id="rejected-is-terminal"),
        pytest.param("Offer", "Withdrawn", True, id="offer-is-terminal"),
        pytest.param("Withdrawn", "Rejected", True, id="withdrawn-is-terminal"),
        pytest.param("Offer", "Offer", False, id="declined-offer-remains-offer"),
    ],
)
def test_terminal_statuses_are_not_replaced_automatically(
    tmp_path: Path,
    current_status: str,
    proposed_status: str,
    requires_review: bool,
) -> None:
    sheet = RecordingApplicationSheet()
    sheet.rows = [
        (
            "spreadsheet-terminal",
            Application(
                company="Example Corp",
                position="Engineer",
                application_date="2026-07-01",
                status=cast(ApplicationStatus, current_status),
            ),
        )
    ]

    with SqliteTrackerState(tmp_path / "tracker.sqlite3") as state:
        state.save_spreadsheet_id("spreadsheet-terminal")
        exit_code = run(
            workspace=ExistingTracker(),
            state=state,
            stdout=StringIO(),
            sync=SyncAdapters(
                mailbox=FakeMailbox(
                    MailboxScan(
                        messages=(
                            GmailMessage(
                                message_id="terminal-update",
                                sender="Example Corp <jobs@example.com>",
                                subject="Update",
                                timestamp="2026-07-24T12:00:00Z",
                                normalized_body="A final decision.",
                            ),
                        ),
                        checkpoint="checkpoint-terminal",
                    )
                ),
                classifier=RecordingClassifier(
                    StatusUpdate(
                        company="Example Corp",
                        position="Engineer",
                        status=cast(ApplicationStatus, proposed_status),
                    )
                ),
                application_sheet=sheet,
                confirm=lambda: True,
            ),
        )

    assert exit_code == 0
    assert sheet.rows[0][1].status == current_status
    assert len(sheet.review_rows) == int(requires_review)


def test_real_gmail_adapter_excludes_attachment_text_from_classifier(
    tmp_path: Path,
) -> None:
    private_attachment_text = "private resume contents"
    gmail_service = FakeGmailService(
        {
            "id": "gmail-private-1",
            "internalDate": "1784912400000",
            "payload": {
                "mimeType": "multipart/mixed",
                "headers": [
                    {
                        "name": "From",
                        "value": "Example Corp <jobs@example.com>",
                    },
                    {
                        "name": "Subject",
                        "value": "Application received",
                    },
                ],
                "parts": [
                    {
                        "mimeType": "text/plain",
                        "filename": "",
                        "body": {
                            "data": encoded_body(
                                "We received your application."
                            )
                        },
                    },
                    {
                        "mimeType": "text/plain",
                        "filename": "",
                        "headers": [
                            {
                                "name": "Content-Disposition",
                                "value": "attachment",
                            }
                        ],
                        "body": {
                            "data": encoded_body(private_attachment_text)
                        },
                    },
                    {
                        "mimeType": "message/rfc822",
                        "filename": "",
                        "body": {},
                        "parts": [
                            {
                                "mimeType": "text/plain",
                                "filename": "",
                                "body": {
                                    "data": encoded_body(
                                        "private attached message"
                                    )
                                },
                            }
                        ],
                    },
                ],
            },
        }
    )
    mailbox = GoogleApiWorkspace(
        GoogleAuthConfig(
            client_secrets_path=tmp_path / "credentials.json",
            token_path=tmp_path / "token.json",
        ),
        service_factory=lambda api_name, api_version: gmail_service,
    )
    classifier = RecordingClassifier(
        Application(
            company="Example Corp",
            position="Software Engineer",
            application_date="2026-07-24",
        )
    )
    sheet = RecordingApplicationSheet()

    with SqliteTrackerState(tmp_path / "tracker.sqlite3") as state:
        state.save_spreadsheet_id("spreadsheet-private")
        exit_code = run(
            workspace=ExistingTracker(),
            state=state,
            stdout=StringIO(),
            sync=SyncAdapters(
                mailbox=mailbox,
                classifier=classifier,
                application_sheet=sheet,
                confirm=lambda: True,
            ),
        )

    assert exit_code == 0
    assert classifier.received[0].normalized_body == (
        "We received your application."
    )
    assert private_attachment_text not in repr(classifier.received)


def test_real_openai_adapter_transmits_only_minimized_email_fields(
    tmp_path: Path,
) -> None:
    client = FakeOpenAIClient()
    classifier = OpenAIApplicationClassifier(client=client)
    message = GmailMessage(
        message_id="gmail-id-must-stay-local",
        sender="Example Corp <jobs@example.com>",
        subject="Application received",
        timestamp="2026-07-24T15:00:00Z",
        normalized_body="We received your application.",
    )
    sheet = RecordingApplicationSheet()

    with SqliteTrackerState(tmp_path / "tracker.sqlite3") as state:
        state.save_spreadsheet_id("spreadsheet-openai")
        exit_code = run(
            workspace=ExistingTracker(),
            state=state,
            stdout=StringIO(),
            sync=SyncAdapters(
                mailbox=FakeMailbox(
                    MailboxScan(
                        messages=(message,),
                        checkpoint="checkpoint-openai",
                    )
                ),
                classifier=classifier,
                application_sheet=sheet,
                confirm=lambda: True,
            ),
        )

    assert exit_code == 0
    request = client.responses.requests[0]
    assert json.loads(request["input"]) == {
        "sender": "Example Corp <jobs@example.com>",
        "subject": "Application received",
        "timestamp": "2026-07-24T15:00:00Z",
        "normalized_body": "We received your application.",
    }
    assert request["store"] is False
    assert "gmail-id-must-stay-local" not in repr(request)


def test_real_openai_adapter_returns_a_conclusive_status_update() -> None:
    client = FakeOpenAIClient()
    client.responses.create = lambda **request: SimpleNamespace(  # type: ignore[method-assign]
        output_text=json.dumps(
            {
                "kind": "status_update",
                "company": "Example Corp",
                "position": "Software Engineer",
                "application_date": "",
                "status": "Rejected",
                "reason": "",
            }
        )
    )

    outcome = OpenAIApplicationClassifier(client=client).classify(
        ClassificationInput(
            sender="Example Corp <jobs@example.com>",
            subject="Update on your application",
            timestamp="2026-07-24T15:00:00Z",
            normalized_body="We selected another candidate.",
        )
    )

    assert outcome == StatusUpdate(
        company="Example Corp",
        position="Software Engineer",
        status="Rejected",
    )


def test_initial_gmail_scan_searches_all_eligible_locations_in_time_order(
    tmp_path: Path,
) -> None:
    gmail_service = RecordingGmailService(
        {
            "later": {
                "internalDate": "1767355200000",
                "payload": {
                    "headers": [
                        {"name": "From", "value": "jobs@example.com"},
                        {"name": "Subject", "value": "Later"},
                    ]
                },
            },
            "earlier": {
                "internalDate": "1767268800000",
                "payload": {
                    "headers": [
                        {"name": "From", "value": "jobs@example.com"},
                        {"name": "Subject", "value": "Earlier"},
                    ]
                },
            },
        }
    )
    mailbox = GoogleApiWorkspace(
        GoogleAuthConfig(
            client_secrets_path=tmp_path / "credentials.json",
            token_path=tmp_path / "token.json",
        ),
        service_factory=lambda api_name, api_version: gmail_service,
    )

    scan = mailbox.find_messages(None)

    assert gmail_service.messages().list_requests == [
        {
            "userId": "me",
            "q": "in:anywhere -in:spam -in:trash",
            "maxResults": 500,
        }
    ]
    assert [message.message_id for message in scan.messages] == [
        "earlier",
        "later",
    ]


def test_initial_command_passes_start_date_to_mailbox(
    tmp_path: Path,
) -> None:
    workspace = SyncingWorkspace(MailboxScan(messages=(), checkpoint="0"))

    exit_code = main(
        [
            "--data-dir",
            str(tmp_path),
            "--start-date",
            "2026-01-02",
        ],
        workspace_factory=lambda config: workspace,
        classifier_factory=lambda: RecordingClassifier(None),
        stdout=StringIO(),
    )

    assert exit_code == 0
    assert workspace.scan_requests == [(None, date(2026, 1, 2))]


def test_start_date_filters_earlier_gmail_history(tmp_path: Path) -> None:
    gmail_service = RecordingGmailService({})
    mailbox = GoogleApiWorkspace(
        GoogleAuthConfig(
            client_secrets_path=tmp_path / "credentials.json",
            token_path=tmp_path / "token.json",
        ),
        service_factory=lambda api_name, api_version: gmail_service,
    )

    mailbox.find_messages(None, start_date=date(2026, 1, 2))

    assert gmail_service.messages().list_requests[0]["q"] == (
        "in:anywhere -in:spam -in:trash after:2026/01/02"
    )


def test_exactly_500_unprocessed_messages_are_classified(
    tmp_path: Path,
) -> None:
    messages = tuple(
        GmailMessage(
            message_id=f"message-{index}",
            sender="jobs@example.com",
            subject="Job update",
            timestamp="2026-01-02T00:00:00Z",
            normalized_body="A hiring update.",
        )
        for index in range(500)
    )
    classifier = RecordingClassifier(None)

    with SqliteTrackerState(tmp_path / "tracker.sqlite3") as state:
        state.save_spreadsheet_id("spreadsheet-500")
        exit_code = run(
            workspace=ExistingTracker(),
            state=state,
            stdout=StringIO(),
            sync=SyncAdapters(
                mailbox=FakeMailbox(
                    MailboxScan(messages=messages, checkpoint="checkpoint-500")
                ),
                classifier=classifier,
                application_sheet=RecordingApplicationSheet(),
                confirm=lambda: True,
            ),
        )

        assert state.get_successful_checkpoint() == "checkpoint-500"

    assert exit_code == 0
    assert len(classifier.received) == 500


def test_large_unprocessed_batch_stops_before_classification_or_state_changes(
    tmp_path: Path,
) -> None:
    messages = tuple(
        GmailMessage(
            message_id=f"message-{index}",
            sender="jobs@example.com",
            subject="Job update",
            timestamp="2026-01-02T00:00:00Z",
            normalized_body="Private email body that must not be printed.",
        )
        for index in range(501)
    )
    classifier = RecordingClassifier(None)
    sheet = RecordingApplicationSheet()
    stdout = StringIO()
    stderr = StringIO()

    with SqliteTrackerState(tmp_path / "tracker.sqlite3") as state:
        state.save_spreadsheet_id("spreadsheet-501")
        exit_code = run(
            workspace=ExistingTracker(),
            state=state,
            stdout=stdout,
            stderr=stderr,
            sync=SyncAdapters(
                mailbox=FakeMailbox(
                    MailboxScan(messages=messages, checkpoint="checkpoint-501")
                ),
                classifier=classifier,
                application_sheet=sheet,
                confirm=lambda: True,
            ),
        )

        assert state.get_successful_checkpoint() is None
        assert not state.has_processed_message("message-0")

    assert exit_code == 1
    assert classifier.received == []
    assert sheet.rows == []
    assert stderr.getvalue() == (
        "Found 501 unprocessed Gmail messages. Re-run with "
        "--allow-large-import to classify this batch.\n"
    )
    assert "Private email body" not in stdout.getvalue()
    assert "Private email body" not in stderr.getvalue()


def test_explicit_override_allows_large_import_to_reach_preview(
    tmp_path: Path,
) -> None:
    messages = tuple(
        GmailMessage(
            message_id=f"message-{index}",
            sender="jobs@example.com",
            subject="Application received",
            timestamp="2026-01-02T00:00:00Z",
            normalized_body="We received your application.",
        )
        for index in range(501)
    )
    workspace = SyncingWorkspace(
        MailboxScan(messages=messages, checkpoint="checkpoint-override")
    )
    classifier = RecordingClassifier(
        Application(
            company="Example Corp",
            position="Engineer",
            application_date="2026-01-02",
        )
    )
    stdout = StringIO()

    exit_code = main(
        ["--data-dir", str(tmp_path), "--allow-large-import"],
        workspace_factory=lambda config: workspace,
        classifier_factory=lambda: classifier,
        stdin=StringIO("no\n"),
        stdout=stdout,
    )

    assert exit_code == 0
    assert len(classifier.received) == 501
    assert "Proposed Applications:\n" in stdout.getvalue()
    assert stdout.getvalue().endswith("Cancelled; no Applications imported.\n")
    assert not workspace.created
    with SqliteTrackerState(tmp_path / "tracker.sqlite3") as state:
        assert state.get_successful_checkpoint() is None
