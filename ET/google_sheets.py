import json
import os

import gspread
from google.oauth2.service_account import Credentials


SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/calendar",
]

EXPECTED_HEADER = ["Date", "Exam", "Event", "Event Type", "Exam URL"]


def deduplicate_sheet_rows(rows):
    """Remove exact duplicate Sheet rows while preserving their first order.

    A multi-day exam remains valid because its date is part of the key. Only
    identical Date, Exam, Event, Event Type and Exam URL values are removed.
    """
    unique_rows = []
    seen = set()
    for row in rows:
        key = tuple(str(row.get(field, "")).strip() for field in (
            "date", "exam", "event", "event_type", "exam_url",
        ))
        if key in seen:
            continue
        seen.add(key)
        unique_rows.append(row)
    return unique_rows


def get_credentials():
    """
    Build and return a google-auth Credentials object from the
    GOOGLE_SERVICE_ACCOUNT_JSON environment variable. Shared by both the
    Sheets client (get_google_sheet) and the Calendar client
    (calendar_sync.py), so both use the exact same service account.
    """
    service_account_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not service_account_json:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON secret is missing")

    try:
        service_account_info = json.loads(service_account_json)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON"
        ) from exc

    return Credentials.from_service_account_info(
        service_account_info,
        scopes=SCOPES,
    )


def get_google_sheet():
    sheet_id = os.environ.get("GOOGLE_SHEET_ID")
    if not sheet_id:
        raise RuntimeError("GOOGLE_SHEET_ID secret is missing")

    credentials = get_credentials()
    client = gspread.authorize(credentials)

    return client.open_by_key(sheet_id)


def write_month_data(rows, month_label):
    original_count = len(rows)
    rows = deduplicate_sheet_rows(rows)
    removed_count = original_count - len(rows)
    if removed_count:
        print(
            f"Removed {removed_count} exact duplicate row(s) before writing "
            f"Google Sheet tab '{month_label}'."
        )

    spreadsheet = get_google_sheet()

    try:
        worksheet = spreadsheet.worksheet(month_label)
        worksheet.clear()
    except gspread.exceptions.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(
            title=month_label,
            rows=500,
            cols=5,
        )

    header = [
        "Date",
        "Exam",
        "Event",
        "Event Type",
        "Exam URL",
    ]

    data = [header] + [
        [
            row["date"],
            row["exam"],
            row["event"],
            row["event_type"],
            row["exam_url"],
        ]
        for row in rows
    ]

    worksheet.update(
        data,
        value_input_option="RAW",
    )

    print(
        f"Successfully wrote {len(rows)} rows "
        f"to Google Sheet tab '{month_label}'."
    )


def read_exam_names():
    """Read the first column of the `Exam_Name` allowlist worksheet."""
    spreadsheet = get_google_sheet()
    try:
        worksheet = spreadsheet.worksheet("Exam_Name")
    except gspread.exceptions.WorksheetNotFound as exc:
        raise RuntimeError("Required Google Sheet worksheet 'Exam_Name' was not found") from exc

    values = worksheet.col_values(1)
    # A header named Exam_Name is conventional but optional; blank rows are ignored
    # by the caller's normalizer.
    if values and values[0].strip().casefold() == "exam_name":
        values = values[1:]
    return values


def read_all_rows(tab_name=None):
    """
    Read exam rows back out of the spreadsheet, for calendar_sync.py.

    If tab_name is given, reads only that worksheet/tab. Otherwise reads
    every worksheet in the spreadsheet (i.e. every month tab written by
    write_month_data) and concatenates the rows.

    Returns a list of dicts: date, exam, event, event_type, exam_url,
    source_tab (which tab it came from — useful for logging).
    """
    spreadsheet = get_google_sheet()
    worksheets = [spreadsheet.worksheet(tab_name)] if tab_name else spreadsheet.worksheets()

    rows = []
    for ws in worksheets:
        values = ws.get_all_values()
        if not values:
            continue
        header, *body = values
        norm_header = [h.strip() for h in header]
        if norm_header != EXPECTED_HEADER:
            # Not an exam-data tab (or header drifted) — skip rather than crash.
            continue
        for r in body:
            if len(r) < 5 or not any(c.strip() for c in r):
                continue
            date, exam, event, event_type, exam_url = (r + ["", "", "", "", ""])[:5]
            rows.append(
                {
                    "date": date.strip(),
                    "exam": exam.strip(),
                    "event": event.strip(),
                    "event_type": event_type.strip(),
                    "exam_url": exam_url.strip(),
                    "source_tab": ws.title,
                }
            )
    return rows
