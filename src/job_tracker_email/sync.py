from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class GmailMessage:
    message_id: str
    sender: str
    subject: str
    timestamp: str
    normalized_body: str


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
    status: Literal["Active"] = "Active"
    stage: str = ""


@dataclass(frozen=True)
class ReviewProposal:
    """Classifier result for candidacy evidence that needs manual review."""

    reason: str


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
