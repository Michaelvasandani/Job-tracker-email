from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

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
    ClassificationInput,
    GmailMessage,
    MailboxScan,
    NeedsReview,
    ReviewProposal,
)
from job_tracker_email.state import SqliteTrackerState


@dataclass
class FakeMailbox:
    scan: MailboxScan

    def find_messages(self, after_checkpoint: str | None) -> MailboxScan:
        return self.scan


class RecordingClassifier:
    def __init__(
        self,
        result: Application | ReviewProposal | None,
    ) -> None:
        self.result = result
        self.received: list[ClassificationInput] = []

    def classify(
        self,
        message: ClassificationInput,
    ) -> Application | ReviewProposal | None:
        self.received.append(message)
        return self.result


class RecordingApplicationSheet:
    def __init__(self) -> None:
        self.rows: list[tuple[str, Application]] = []
        self.review_rows: list[tuple[str, NeedsReview]] = []

    def append_application(
        self,
        spreadsheet_id: str,
        application: Application,
    ) -> None:
        self.rows.append((spreadsheet_id, application))

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


class FailOnceApplicationSheet(RecordingApplicationSheet):
    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    def append_application(
        self,
        spreadsheet_id: str,
        application: Application,
    ) -> None:
        self.attempts += 1
        super().append_application(spreadsheet_id, application)
        if self.attempts == 1:
            raise RuntimeError(
                "write failed while handling private-email-body"
            )


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

    def create_spreadsheet(self, definition: object) -> str:
        self.created = True
        return "spreadsheet-from-main"

    def find_messages(self, after_checkpoint: str | None) -> MailboxScan:
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
