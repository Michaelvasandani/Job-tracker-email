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
