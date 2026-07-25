from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


ApplicationStatus = Literal["Active", "Rejected", "Offer", "Withdrawn"]
APPLICATION_STATUSES: frozenset[ApplicationStatus] = frozenset(
    {"Active", "Rejected", "Offer", "Withdrawn"}
)


def parse_application_status(value: str) -> ApplicationStatus | None:
    if value not in APPLICATION_STATUSES:
        return None
    return value


@dataclass(frozen=True)
class GmailMessage:
    message_id: str
    sender: str
    subject: str
    timestamp: str
    normalized_body: str
    thread_id: str = ""


@dataclass(frozen=True)
class MailboxScan:
    messages: tuple[GmailMessage, ...]
    checkpoint: str


@dataclass(frozen=True)
class ClassificationInput:
    sender: str
    subject: str
    timestamp: str
    normalized_body: str


@dataclass(frozen=True)
class Application:
    company: str
    position: str
    application_date: str
    status: ApplicationStatus = "Active"
    stage: str = ""


@dataclass(frozen=True)
class ReviewProposal:
    """Classifier result for candidacy evidence that needs manual review."""

    reason: str


@dataclass(frozen=True)
class StatusUpdate:
    """A conclusive outcome for one existing Application."""

    company: str
    position: str
    status: ApplicationStatus


@dataclass(frozen=True)
class NeedsReview:
    email_date: str
    sender: str
    subject: str
    gmail_link: str
    reason: str


@dataclass(frozen=True)
class PendingApplicationWrite:
    application: Application
    matching_rows_before_write: int


@dataclass(frozen=True)
class PendingStatusUpdate:
    row_number: int
    status: ApplicationStatus


@dataclass(frozen=True)
class ThreadApplication:
    row_number: int
    company: str
    position: str
    application_date: str
