from __future__ import annotations

import json
from datetime import date
from typing import Any

from job_tracker_email.sync import Application, ClassificationInput


CLASSIFICATION_INSTRUCTIONS = """
Classify one English-language email for a private job-application tracker.
Return new_application only when the message directly confirms that the user
submitted or entered candidacy for a position. Company is the prospective
employer, never an applicant-tracking platform or incidental sender. Position
is the human-readable role title with a meaningful level, excluding location,
department, and requisition identifiers. Application date must come from
direct submission evidence. Return other with empty derived fields when the
message is irrelevant, unclear, unsupported, or lacks any required fact.
""".strip()


CLASSIFICATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "enum": ["new_application", "other"],
        },
        "company": {"type": "string"},
        "position": {"type": "string"},
        "application_date": {"type": "string"},
    },
    "required": [
        "kind",
        "company",
        "position",
        "application_date",
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
    ) -> Application | None:
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
        if result["kind"] != "new_application":
            return None

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
