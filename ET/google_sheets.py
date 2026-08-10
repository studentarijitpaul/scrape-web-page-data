import json
import os

import gspread
from google.oauth2.service_account import Credentials


SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]


def get_google_sheet():
    service_account_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    sheet_id = os.environ.get("GOOGLE_SHEET_ID")

    if not service_account_json:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON secret is missing")

    if not sheet_id:
        raise RuntimeError("GOOGLE_SHEET_ID secret is missing")

    try:
        service_account_info = json.loads(service_account_json)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON"
        ) from exc

    credentials = Credentials.from_service_account_info(
        service_account_info,
        scopes=SCOPES,
    )

    client = gspread.authorize(credentials)

    return client.open_by_key(sheet_id)


def write_month_data(rows, month_label):
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
