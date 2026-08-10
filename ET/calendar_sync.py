from __future__ import annotations

import hashlib
import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from google_sheets import get_credentials, read_all_rows


# ============================================================
# CONFIGURATION
# ============================================================

DEFAULT_TIMEZONE = "Asia/Kolkata"

SYNC_SOURCE_TAG = "shiksha_exam_sync"

# IMPORTANT:
# Google Calendar custom event IDs must use only:
# a-v and 0-9
#
# "exam" was invalid because it contains "x".
#
EVENT_ID_PREFIX = "evt"


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

log = logging.getLogger("calendar_sync")


# ============================================================
# SYNC STATISTICS
# ============================================================

@dataclass
class SyncStats:
    created: int = 0
    updated: int = 0
    skipped: int = 0
    deleted: int = 0
    failed: int = 0

    def summary(self) -> str:
        return (
            f"created={self.created} "
            f"updated={self.updated} "
            f"skipped={self.skipped} "
            f"deleted={self.deleted} "
            f"failed={self.failed}"
        )


# ============================================================
# CALENDAR ID
# ============================================================

def validate_and_clean_calendar_id(calendar_id: str) -> str:
    """
    Clean and validate GOOGLE_CALENDAR_ID.
    """

    if not calendar_id:
        raise RuntimeError(
            "GOOGLE_CALENDAR_ID is not set."
        )

    # Remove whitespace/newlines
    cleaned = calendar_id.strip()

    # Remove surrounding quotes
    cleaned = re.sub(
        r'^["\']|["\']$',
        '',
        cleaned
    ).strip()

    if not cleaned:
        raise RuntimeError(
            "GOOGLE_CALENDAR_ID is empty."
        )

    if "@" not in cleaned:
        raise RuntimeError(
            "GOOGLE_CALENDAR_ID appears invalid. "
            "Expected something like "
            "abc123@group.calendar.google.com"
        )

    log.info(
        "Calendar ID detected: ...%s",
        cleaned[-30:]
    )

    return cleaned


# ============================================================
# EVENT ID
# ============================================================

def make_event_id(
    exam: str,
    date_str: str,
    event_type: str
) -> str:
    """
    Generate a deterministic Google Calendar event ID.

    The generated ID contains only characters allowed by
    Google Calendar custom event IDs.
    """

    key = (
        f"{exam.strip().lower()}|"
        f"{date_str.strip()}|"
        f"{event_type.strip().lower()}"
    )

    digest = hashlib.md5(
        key.encode("utf-8")
    ).hexdigest()

    event_id = f"{EVENT_ID_PREFIX}{digest}"

    # Google Calendar event IDs must contain only:
    # a-v and 0-9
    if not re.fullmatch(
        r"[a-v0-9]{5,1024}",
        event_id
    ):
        raise ValueError(
            f"Generated invalid Google Calendar event ID: "
            f"{event_id}"
        )

    return event_id


# ============================================================
# DATE
# ============================================================

def parse_row_date(
    date_str: str
) -> Optional[date]:
    """
    Convert YYYY-MM-DD string into a date object.
    """

    date_str = (date_str or "").strip()

    if not date_str:
        return None

    try:
        return datetime.strptime(
            date_str,
            "%Y-%m-%d"
        ).date()

    except ValueError:
        return None


# ============================================================
# CALENDAR SERVICE
# ============================================================

def build_calendar_service():
    """
    Build Google Calendar API service using the
    credentials provided by google_sheets.py.
    """

    creds = get_credentials()

    return build(
        "calendar",
        "v3",
        credentials=creds,
        cache_discovery=False
    )


# ============================================================
# TEST CALENDAR ACCESS
# ============================================================

def test_calendar_access(
    service,
    calendar_id: str
):
    """
    Test whether the service account can access
    the specified Google Calendar.
    """

    log.info(
        "Testing Google Calendar access..."
    )

    try:

        calendar = (
            service.calendars()
            .get(calendarId=calendar_id)
            .execute()
        )

        calendar_name = calendar.get(
            "summary",
            "(unknown)"
        )

        timezone = calendar.get(
            "timeZone",
            "(unknown)"
        )

        log.info(
            "Google Calendar connection successful!"
        )

        log.info(
            "Calendar name: %s",
            calendar_name
        )

        log.info(
            "Calendar timezone: %s",
            timezone
        )

    except HttpError as e:

        log.error(
            "Unable to access Google Calendar."
        )

        log.error(
            "Calendar ID used: ...%s",
            calendar_id[-30:]
        )

        log.error(
            "Google Calendar API error: %s",
            e
        )

        raise RuntimeError(
            "The service account cannot access the specified "
            "Google Calendar. Check GOOGLE_CALENDAR_ID and "
            "calendar sharing permissions."
        ) from e


# ============================================================
# EVENT BODY
# ============================================================

def row_to_event_body(row: dict) -> dict:
    """
    Convert a Google Sheet row into a Google Calendar event.
    """

    d = parse_row_date(
        row.get("date", "")
    )

    if d is None:
        raise ValueError(
            f"Invalid date: {row.get('date')}"
        )

    # Google Calendar uses an exclusive end date
    # for all-day events.
    end = d + timedelta(days=1)

    description_lines = []

    if row.get("event"):
        description_lines.append(
            str(row["event"])
        )

    if row.get("event_type"):
        description_lines.append(
            f"Type: {row['event_type']}"
        )

    if row.get("exam_url"):
        description_lines.append(
            f"More info: {row['exam_url']}"
        )

    if row.get("source_tab"):
        description_lines.append(
            f"Source sheet tab: {row['source_tab']}"
        )

    event_id = make_event_id(
        row["exam"],
        row["date"],
        row.get("event_type", "")
    )

    body = {

        "summary": row["exam"] or "Exam event",

        "description": "\n".join(
            description_lines
        ),

        "start": {
            "date": d.isoformat()
        },

        "end": {
            "date": end.isoformat()
        },

        "extendedProperties": {

            "private": {

                "sync_source": SYNC_SOURCE_TAG,

                "row_key": event_id

            }

        }
    }

    if row.get("exam_url"):

        body["source"] = {
            "title": "Exam details",
            "url": row["exam_url"]
        }

    return body


# ============================================================
# CREATE / UPDATE
# ============================================================

def upsert_event(
    service,
    calendar_id: str,
    event_id: str,
    body: dict
) -> str:
    """
    Create an event.

    If the event already exists, update it instead.
    """

    body = dict(body)

    body["id"] = event_id

    try:

        service.events().insert(
            calendarId=calendar_id,
            body=body
        ).execute()

        return "created"

    except HttpError as e:

        # Event already exists
        if e.resp.status == 409:

            service.events().update(
                calendarId=calendar_id,
                eventId=event_id,
                body=body
            ).execute()

            return "updated"

        raise


# ============================================================
# DELETE EVENT
# ============================================================

def delete_event(
    service,
    calendar_id: str,
    event_id: str
):
    """
    Delete a Google Calendar event.

    410 means the event is already gone, so it is ignored.
    """

    try:

        service.events().delete(
            calendarId=calendar_id,
            eventId=event_id
        ).execute()

    except HttpError as e:

        if e.resp.status != 410:
            raise


# ============================================================
# FIND EXISTING SYNC EVENTS
# ============================================================

def list_existing_synced_event_ids(
    service,
    calendar_id: str
) -> set[str]:
    """
    Find all events previously created by this sync script.
    """

    ids = set()

    page_token = None

    while True:

        response = (
            service.events()
            .list(
                calendarId=calendar_id,
                privateExtendedProperty=(
                    f"sync_source={SYNC_SOURCE_TAG}"
                ),
                showDeleted=False,
                singleEvents=True,
                pageToken=page_token,
                maxResults=2500,
            )
            .execute()
        )

        for item in response.get(
            "items",
            []
        ):

            if item.get("id"):
                ids.add(item["id"])

        page_token = response.get(
            "nextPageToken"
        )

        if not page_token:
            break

    return ids


# ============================================================
# MAIN SYNC
# ============================================================

def run_sync() -> SyncStats:

    stats = SyncStats()

    # --------------------------------------------------------
    # Calendar ID
    # --------------------------------------------------------

    calendar_id = validate_and_clean_calendar_id(
        os.environ.get(
            "GOOGLE_CALENDAR_ID",
            ""
        )
    )

    tab_name = (
        os.environ.get(
            "SHEET_TAB_NAME"
        )
        or None
    )

    delete_removed = (
        os.environ.get(
            "DELETE_REMOVED_EVENTS",
            "false"
        )
        .strip()
        .lower()
        == "true"
    )

    # --------------------------------------------------------
    # Read Google Sheet
    # --------------------------------------------------------

    log.info(
        "Reading rows%s from Google Sheet...",
        (
            f" from tab '{tab_name}'"
            if tab_name
            else " (all tabs)"
        )
    )

    rows = read_all_rows(
        tab_name=tab_name
    )

    log.info(
        "Read %d row(s).",
        len(rows)
    )

    # --------------------------------------------------------
    # Build Calendar service
    # --------------------------------------------------------

    service = build_calendar_service()

    # --------------------------------------------------------
    # Test Calendar BEFORE processing rows
    # --------------------------------------------------------

    test_calendar_access(
        service,
        calendar_id
    )

    desired_ids = set()

    # --------------------------------------------------------
    # Sync rows
    # --------------------------------------------------------

    for row in rows:

        d = parse_row_date(
            row.get("date", "")
        )

        if d is None:

            log.warning(
                "Skipping invalid date: %r",
                row.get("date")
            )

            stats.skipped += 1

            continue

        if not row.get("exam"):

            log.warning(
                "Skipping row with empty exam name: %r",
                row
            )

            stats.skipped += 1

            continue

        # ----------------------------------------------------
        # Generate deterministic event ID
        # ----------------------------------------------------

        event_id = make_event_id(
            row["exam"],
            row["date"],
            row.get("event_type", "")
        )

        desired_ids.add(event_id)

        # ----------------------------------------------------
        # Build event body
        # ----------------------------------------------------

        body = row_to_event_body(row)

        # ----------------------------------------------------
        # Create / update event
        # ----------------------------------------------------

        try:

            action = upsert_event(
                service,
                calendar_id,
                event_id,
                body
            )

            if action == "created":

                stats.created += 1

                log.info(
                    "Created: %s (%s)",
                    row["exam"],
                    row["date"]
                )

            else:

                stats.updated += 1

                log.info(
                    "Updated: %s (%s)",
                    row["exam"],
                    row["date"]
                )

        except HttpError as e:

            stats.failed += 1

            log.error(
                "Failed to sync '%s' (%s): %s",
                row["exam"],
                row["date"],
                e
            )

    # --------------------------------------------------------
    # Delete removed events
    # --------------------------------------------------------

    if delete_removed:

        log.info(
            "DELETE_REMOVED_EVENTS enabled."
        )

        existing_ids = (
            list_existing_synced_event_ids(
                service,
                calendar_id
            )
        )

        for event_id in (
            existing_ids - desired_ids
        ):

            try:

                delete_event(
                    service,
                    calendar_id,
                    event_id
                )

                stats.deleted += 1

                log.info(
                    "Deleted stale event: %s",
                    event_id
                )

            except HttpError as e:

                stats.failed += 1

                log.error(
                    "Failed to delete stale event %s: %s",
                    event_id,
                    e
                )

    else:

        log.info(
            "DELETE_REMOVED_EVENTS disabled."
        )

    return stats


# ============================================================
# ENTRY POINT
# ============================================================

def main():

    try:

        stats = run_sync()

    except RuntimeError as e:

        log.error(
            "Configuration error: %s",
            e
        )

        sys.exit(1)

    except Exception as e:

        log.error(
            "Unexpected error during sync: %s",
            e,
            exc_info=True
        )

        sys.exit(1)

    log.info(
        "Sync complete: %s",
        stats.summary()
    )

    if stats.failed > 0:

        log.error(
            "%d row(s)/event(s) failed.",
            stats.failed
        )

        sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    main()
