# Manual smoke test: one confirmed Application

Use a clean Google test account whose mailbox contains no personal or
sensitive mail. This keeps the smoke test to one Gmail read and one OpenAI
classification while the historical-scope controls are still being built.

## Prepare

1. Enable the Gmail API and Google Sheets API in a Google Cloud project.
2. Create an OAuth client of type **Desktop app** and download its JSON file.
3. Set an OpenAI API key in the shell:

   ```bash
   export OPENAI_API_KEY="your-api-key"
   ```

4. From a second test account, send the Google test account one email with no
   attachment:

   - Subject: `Application received`
   - Body: `Thank you for applying to Example Company. We received your
     application for Senior Software Engineer on July 24, 2026.`

Do not use a real employer, role, candidate name, résumé, or other personal
information.

## Run

Use a new private data directory outside the repository:

```bash
job-tracker-email \
  --client-secrets /absolute/path/to/desktop-client.json \
  --data-dir /absolute/path/to/job-tracker-smoke-data
```

In the browser:

1. Sign in with the clean Google test account.
2. Confirm that the consent screen requests read-only Gmail access and access
   to files created by this application.

In the terminal:

1. Verify the preview says Company `Example Company`, Position
   `Senior Software Engineer`, Application Date `2026-07-24`, Status `Active`,
   and a blank Stage.
2. Enter `yes` only if all five values are correct.
3. Confirm the command prints `Application imported.`

## Verify

Open the created `Job Application Tracker` spreadsheet and confirm:

- `Applications` contains one data row with exactly the previewed values.
- Stage is blank.
- No sender, subject, email body, or attachment content is present.

Run the same command again. Confirm that it does not append a duplicate row.
The successful first run should have cached Google authorization locally, so
the second run should not require another consent flow unless Google requires
reauthorization.

The smoke test passes when one non-sensitive Gmail message is read, one
structured OpenAI classification is previewed, one approved row is appended,
the successful SQLite checkpoint is recorded, and a rerun creates no
duplicate.
