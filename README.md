# Job Application Email Tracker

A private, manually triggered Python command that reads Gmail submission
confirmations, previews derived job Applications, and appends approved rows to
a dedicated Google spreadsheet.

The command uses one installed-app Google OAuth grant, one cached token, local
SQLite state, and one spreadsheet with `Applications`, `Needs Review`, and
`Stats` tabs. Clear English submission confirmations are classified with
OpenAI structured outputs. Every proposed Application must be approved before
an `Applications` row or successful-sync checkpoint is written.

## Requirements

- Python 3.11 or newer
- A Google Cloud project with the Gmail API and Google Sheets API enabled
- An OAuth 2.0 client configured as a **Desktop app**
- An OpenAI API key

The command requests only these Google permissions:

- read-only Gmail access
- per-file permission to create and maintain the app-created tracker

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e .
```

Download the desktop OAuth client file from Google Cloud. Either place it at
`~/.local/share/job-tracker-email/credentials.json` or pass its path explicitly:

```bash
export OPENAI_API_KEY="your-api-key"
job-tracker-email --client-secrets /path/to/credentials.json
```

The first run opens Google's installed-application authorization flow and
creates `Job Application Tracker`. The command reads eligible Gmail messages
in chronological order and sends only sender, subject, timestamp, and
normalized plain-text body to OpenAI. It never opens or transmits attachments.

The initial scan includes all eligible Gmail history by default, including
archived and sent messages, while always excluding spam and trash. To limit
that first scan, provide the earliest date to include:

```bash
job-tracker-email --start-date 2026-01-01
```

To prevent unexpected OpenAI usage, the command stops before classification
when it finds more than 500 unprocessed messages. After reviewing the reported
count, deliberately continue a known large import with:

```bash
job-tracker-email --allow-large-import
```

To conservatively rebuild local bookkeeping from all eligible Gmail history,
run a full rescan. It preserves existing Application rows, routes unmatched or
ambiguous historical evidence to Needs Review, and uses the same confirmation
step as a normal sync:

```bash
job-tracker-email --full-rescan
```

If the local state database was lost, provide the existing tracker spreadsheet
ID so the command can recover against that Sheet instead of creating another:

```bash
job-tracker-email --full-rescan --spreadsheet-id your-spreadsheet-id
```

For each clearly confirmed Application, the command previews Company,
Position, Application Date, Status, and Stage. Enter `y` or `yes` to append the
row. Any other response cancels the batch without advancing the checkpoint.
Later runs reuse the spreadsheet ID and successful checkpoint from local
SQLite state. By default, cached authorization and state are stored under
`~/.local/share/job-tracker-email/`.

To choose a different private data directory:

```bash
job-tracker-email \
  --client-secrets /path/to/credentials.json \
  --data-dir /path/to/private/job-tracker-data
```

The equivalent environment variables are
`JOB_TRACKER_GOOGLE_CLIENT_SECRETS` and `JOB_TRACKER_DATA_DIR`.

Raw email bodies are not written to the spreadsheet, SQLite state, or normal
console output. OpenAI Responses are requested with server-side storage
disabled. Spreadsheet-write errors leave the checkpoint unchanged so the
command can be retried. Per-message pending-write bookkeeping distinguishes a
retry from a separate submission and prevents duplicates after an ambiguous
write result.

For a live end-to-end verification, follow the
[manual smoke test](docs/manual-smoke-test.md).

## Develop

```bash
python3 -m pip install -e ".[dev]"
pytest -p no:capture
mypy src tests
```

`-p no:capture` works around a crash in the current repository environment's
global pytest capture plugin; it is not required in a normal virtual
environment.
