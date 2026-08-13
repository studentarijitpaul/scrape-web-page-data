# Shiksha Exam Calendar Sync

The Shiksha page is the source of current data. The `Exam_Name` worksheet is a strict, normalized allowlist; only matching entries are written to the `August 2026` worksheet, synchronized to Calendar, and notified in Chat. That worksheet is the previous synchronized state used for change detection.

## Setup

Use Python 3.12 (or a compatible Python 3 release), create a virtual environment, then run `pip install -r requirements.txt` and `playwright install chromium`. Share the Google Sheet and Calendar with the service-account email. Create a Google Chat incoming webhook.

Set `GOOGLE_SERVICE_ACCOUNT_JSON`, `GOOGLE_SHEET_ID`, `GOOGLE_CALENDAR_ID`, and `GOOGLE_CHAT_WEBHOOK_URL`, then run `python main.py`.

The Sheet requires an `Exam_Name` tab with a first-column `Exam_Name` header and allowed names below it. The `August 2026` tab is created/updated with the existing five columns: Date, Exam, Event, Event Type, Exam URL.

## Change behavior

The stable identity is normalized `exam + event type`, never the date. Existing sheet rows are classified as new, updated, unchanged, or removed before the sheet is written. Only new and updated entries produce Chat messages; date changes include both dates. Calendar events are created once and updated in place when controlled fields change. Removed Calendar events remain untouched unless `DELETE_REMOVED_EVENTS=true` is explicitly configured.

## Notify Google Chat when somebody edits the Sheet

The Python workflow can notify Chat about changes it makes itself. To receive an immediate message when a person directly edits, pastes into, or clears a cell in the Google Sheet, install the included Apps Script trigger:

1. Open the target Google Sheet and choose **Extensions → Apps Script**.
2. Create a script file, paste in `google_sheets_chat_notifier.gs`, and save it.
3. In **Project Settings → Script properties**, add `GOOGLE_CHAT_WEBHOOK_URL` and set it to the incoming-webhook URL for the target Google Chat space. Do not put the URL directly in the script.
4. In the left sidebar, open **Triggers**, choose **Add Trigger**, and configure:
   - Function: `notifyGoogleChatOnEdit`
   - Event source: **From spreadsheet**
   - Event type: **On edit**
5. Save and complete Google's authorization prompts. Edit a test cell to confirm that the Chat space receives a message.

The trigger sends the sheet tab, changed cell/range, editor (when Google supplies it), and a small preview of the current values. It is designed for human edits. Google Sheets API/service-account writes (including this repository's scraper) do not fire Apps Script edit triggers, so existing automation notifications are not duplicated.

## GitHub Actions

The workflow can be run manually with **Run workflow** and runs daily at 10:00 AM IST (`30 4 * * *`; Actions cron uses UTC). Configure repository secrets: `GOOGLE_SERVICE_ACCOUNT_JSON`, `GOOGLE_SHEET_ID`, `GOOGLE_CALENDAR_ID`, and `GOOGLE_CHAT_WEBHOOK_URL`. View failures and safe logs in the workflow run.

## Tests

Run `python -m pytest -q`. The tests cover name normalization, duplicate and blank allowlist values, filtering, and new/updated/unchanged/removed classification. For an end-to-end check, run `python main.py` after placing a controlled test name in `Exam_Name`; repeat with no changes, then alter a date in the source/test fixture and verify the Calendar event updates and one Chat update message is posted.
