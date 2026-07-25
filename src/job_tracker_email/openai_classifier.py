from __future__ import annotations

import json
from datetime import date
from typing import Any

from job_tracker_email.sync import (
    Application,
    ClassificationInput,
    ReviewProposal,
    StatusUpdate,
    parse_application_status,
)


CLASSIFICATION_INSTRUCTIONS = """
Classify one email for a private job-application tracker. If the email is not
clearly English, return needs_review rather than inferring its meaning.
Return new_application only when the message directly confirms that the user
submitted or entered candidacy for a position. Company is the prospective
employer, never an applicant-tracking platform or incidental sender. Position
is the human-readable role title with a meaningful level, excluding location,
department, and requisition identifiers. Application date must come from
direct submission evidence.

Return ignore for generic job alerts, saved-job reminders, recruiter outreach
before an application, and messages about started, saved, incomplete, or
unsubmitted applications. Return needs_review for every plausible candidacy
message whose Company, Position, Application Date, Application identity, or
Status is uncertain; for later hiring-process messages that cannot identify an
existing Application for a Status update; for unsupported or unclear language; and for an
alternate Position without separate submission or candidacy evidence. Give a
concise reason for needs_review. Do not use a later hiring-message date as an
Application Date. Ignore and new_application must have an empty reason.

Return status_update only when a later message conclusively identifies an
existing Application's Company and Position. Use Active for interviews and
advancement, Rejected for explicit rejection or a filled, closed, or cancelled
Position, Offer only for an explicit employment offer, and Withdrawn only when
the applicant ends an active candidacy before an offer. A declined offer is an
Offer update. If identity or Status is uncertain, return needs_review.
""".strip()


CLASSIFICATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "enum": [
                "new_application",
                "status_update",
                "needs_review",
                "ignore",
            ],
        },
        "company": {"type": "string"},
        "position": {"type": "string"},
        "application_date": {"type": "string"},
        "status": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": [
        "kind",
        "company",
        "position",
        "application_date",
        "status",
        "reason",
    ],
    "additionalProperties": False,
}


class OpenAIApplicationClassifier:
    """OpenAI structured-output boundary for one sanitized email."""

    def __init__(
        self,
        model: str = "gpt-5.6-luna",
        *,
        client: Any | None = None,
    ) -> None:
        self._model = model
        self._provided_client = client

    def classify(
        self,
        message: ClassificationInput,
    ) -> Application | ReviewProposal | StatusUpdate | None:
        client = (
            self._provided_client
            if self._provided_client is not None
            else self._create_client()
        )
        response = client.responses.create(
            model=self._model,
            reasoning={"effort": "none"},
            instructions=CLASSIFICATION_INSTRUCTIONS,
            input=json.dumps(
                {
                    "sender": message.sender,
                    "subject": message.subject,
                    "timestamp": message.timestamp,
                    "normalized_body": message.normalized_body,
                },
                ensure_ascii=False,
            ),
            text={
                "format": {
                    "type": "json_schema",
                    "name": "application_classification",
                    "strict": True,
                    "schema": CLASSIFICATION_SCHEMA,
                }
            },
            store=False,
        )
        result = json.loads(response.output_text)
        if result["kind"] == "ignore":
            return None
        if result["kind"] == "needs_review":
            reason = str(result["reason"]).strip()
            if not reason:
                raise RuntimeError(
                    "OpenAI classification omitted a review reason."
                )
            return ReviewProposal(reason=reason)
        if result["kind"] == "status_update":
            company = str(result["company"]).strip()
            position = str(result["position"]).strip()
            status = parse_application_status(str(result["status"]).strip())
            if not company or not position:
                raise RuntimeError(
                    "OpenAI classification omitted Status update identity."
                )
            if status is None:
                raise RuntimeError(
                    "OpenAI classification returned an invalid Status."
                )
            return StatusUpdate(
                company=company,
                position=position,
                status=status,
            )
        if result["kind"] != "new_application":
            raise RuntimeError("OpenAI classification returned an unknown kind.")

        company = str(result["company"]).strip()
        position = str(result["position"]).strip()
        application_date = str(result["application_date"]).strip()
        if not company or not position:
            raise RuntimeError(
                "OpenAI classification omitted required Application fields."
            )
        date.fromisoformat(application_date)
        return Application(
            company=company,
            position=position,
            application_date=application_date,
        )

    @staticmethod
    def _create_client() -> Any:
        try:
            from openai import OpenAI
        except ImportError as error:
            raise RuntimeError(
                "The OpenAI client library is not installed."
            ) from error
        return OpenAI()
