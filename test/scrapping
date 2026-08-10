import os
import json
import gspread

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build


# ==========================================
# CONFIG
# ==========================================

SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
CALENDAR_ID = os.environ["GOOGLE_CALENDAR_ID"]

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/calendar"
]


# ==========================================
# GOOGLE AUTHENTICATION
# ==========================================

service_account_json = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]

credentials_info = json.loads(service_account_json)

credentials = Credentials.from_service_account_info(
    credentials_info,
    scopes=SCOPES
)


# ==========================================
# CONNECT TO GOOGLE SHEETS
# ==========================================

gc = gspread.authorize(credentials)

spreadsheet = gc.open_by_key(SHEET_ID)

worksheet = spreadsheet.sheet1

rows = worksheet.get_all_records()

print(f"Found {len(rows)} rows in Google Sheet")


# ==========================================
# CONNECT TO GOOGLE CALENDAR
# ==========================================

calendar_service = build(
    "calendar",
    "v3",
    credentials=credentials
)


# ==========================================
# CREATE EVENTS
# ==========================================

for row in rows:

    date = str(row.get("Date", "")).strip()
    label = str(row.get("Label", "")).strip()
    event_name = str(row.get("Event", "")).strip()

    # Skip incomplete rows
    if not date or not event_name:
        continue

    title = event_name

    if label:
        title = f"{label}: {event_name}"

    calendar_event = {
        "summary": title,

        "description": (
            f"Exam Calendar Event\n\n"
            f"Label: {label}\n"
            f"Event: {event_name}\n"
            f"Source: Google Sheet"
        ),

        "start": {
            "date": date
        },

        "end": {
            "date": date
        }
    }

    calendar_service.events().insert(
        calendarId=CALENDAR_ID,
        body=calendar_event
    ).execute()

    print(f"Created calendar event: {title} ({date})")


print("Google Calendar update completed successfully!")
