# Job Application Email Tracker

A private, manually triggered Python command that creates and reuses a
dedicated Google spreadsheet for tracking job Applications.

This first implementation establishes the local foundation: one installed-app
Google OAuth grant, one cached token, one SQLite state database, and one
spreadsheet with `Applications`, `Needs Review`, and `Stats` tabs. Email
classification and synchronization are planned in the later tickets under
`.scratch/job-application-email-tracker/issues/`.

## Requirements

- Python 3.11 or newer
- A Google Cloud project with the Gmail API and Google Sheets API enabled
- An OAuth 2.0 client configured as a **Desktop app**

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
job-tracker-email --client-secrets /path/to/credentials.json
```

The first run opens Google's installed-application authorization flow and
creates `Job Application Tracker`. Later runs reuse its ID from local SQLite
state. By default, cached authorization and state are stored under
`~/.local/share/job-tracker-email/`.

To choose a different private data directory:

```bash
job-tracker-email \
  --client-secrets /path/to/credentials.json \
  --data-dir /path/to/private/job-tracker-data
```

The equivalent environment variables are
`JOB_TRACKER_GOOGLE_CLIENT_SECRETS` and `JOB_TRACKER_DATA_DIR`.

## Develop

```bash
python3 -m pip install -e ".[dev]"
pytest -p no:capture
mypy src tests
```

`-p no:capture` works around a crash in the current repository environment's
global pytest capture plugin; it is not required in a normal virtual
environment.
