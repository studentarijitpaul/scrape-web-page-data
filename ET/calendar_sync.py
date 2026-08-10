"""
calendar_sync.py
Reads exam rows out of the Google Sheet (via google_sheets.py) and syncs
them into a Google Calendar — creating new events, updating changed ones,
and (optionally) deleting events that disappeared from the Sheet.

Idempotent by design: every Sheet row maps to a deterministic Calendar
event ID (a hash of exam name + date + event type). Re-running the sync
with unchanged data updates the same event IDs instead of duplicating them.

Environment variables:
  GOOGLE_SERVICE_ACCOUNT_JSON   Service-account JSON key (required)
  GOOGLE_SHEET_ID               Source spreadsheet ID (required)
  GOOGLE_CALENDAR_ID            Target calendar ID (required)
  SHEET_TAB_NAME                Optional — sync only this one tab instead
                                 of every tab in the spreadsheet.
  SYNC_TIMEZONE                 Optional — default "Asia/Kolkata".
  DELETE_REMOVED_EVENTS         Optional — "true"/"false", default "false".
                                 If true, previously-synced Calendar events
                                 no longer present in the Sheet are deleted.

Exit codes:
  0  success (including "zero changes needed")
  1  configuration/authentication error — nothing was synced
  2  sync ran, but one or more individual rows/events failed
"""

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

DEFAULT_TIMEZONE = "Asia/Kolkata"
SYNC_SOURCE_TAG = "shiksha_exam_sync"
EVENT_ID_PREFIX = "exam"  # Calendar event IDs must be lowercase base32hex (0-9a-v)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("calendar_sync")


@dataclass
class SyncStats:
    created: int = 0
    updated: int = 0
    skipped: int = 0
    deleted: int = 0
    failed: int = 0

    def summary(self) -> str:
        return (
            f"created={self.created} updated={self.updated} "
            f"skipped={self.skipped} deleted={self.deleted} failed={self.failed}"
        )


def make_event_id(exam: str, date_str: str, event_type: str) -> str:
    """Deterministic Calendar event ID for a row's identity (md5 hex = valid base32hex)."""
    key = f"{exam.strip().lower()}|{date_str.strip()}|{event_type.strip().lower()}"
    digest = hashlib.md5(key.encode("utf-8")).hexdigest()
    return f"{EVENT_ID_PREFIX}{digest}"


def parse_row_date(date_str: str) -> Optional[date]:
    """Parse 'YYYY-MM-DD'. Returns None (rather than raising) on bad input."""
    date_str = (date_str or "").strip()
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return None


def validate_and_clean_calendar_id(calendar_id: str) -> str:
    """
    Validate and clean the calendar ID.
    
    Google Calendar IDs typically look like:
    - abc123def456@group.calendar.google.com
    - c_abc123def456@group.calendar.google.com
    
    This function:
    1. Strips whitespace and newlines
    2. Removes any surrounding quotes
    3. Validates it's not empty
    4. Checks it contains @ (basic validation)
    
    Returns the cleaned ID or raises RuntimeError with details.
    """
    if not calendar_id:
        raise RuntimeError("GOOGLE_CALENDAR_ID environment variable is not set.")
    
    # Strip all types of whitespace (spaces, tabs, newlines, etc.)
    cleaned = calendar_id.strip()
    
    # Remove surrounding quotes if present (handles both single and double)
    cleaned = re.sub(r'^["\']|["\']$', '', cleaned)
    
    # Strip again after quote removal
    cleaned = cleaned.strip()
    
    if not cleaned:
        raise RuntimeError(
            "GOOGLE_CALENDAR_ID is empty after stripping whitespace and quotes. "
            "Please set a valid Google Calendar ID (e.g., abc123@group.calendar.google.com)."
        )
    
    if "@" not in cleaned:
        raise RuntimeError(
            f"GOOGLE_CALENDAR_ID appears invalid (missing @): {cleaned[:20]}... "
            "Expected format: abc123@group.calendar.google.com"
        )
    
    # Log the cleaned ID (last 10 chars to avoid exposing full ID)
    log.debug(f"Calendar ID validated (ends with: ...{cleaned[-10:]})")
    
    return cleaned


def row_to_event_body(row: dict) -> dict:
    """Build the Calendar API event body for one Sheet row (all-day event)."""
    d = parse_row_date(row["date"])
    end = d + timedelta(days=1)  # all-day events use an exclusive end date

    description_lines = []
    if row.get("event"):
        description_lines.append(row["event"])
    if row.get("event_type"):
        description_lines.append(f"Type: {row['event_type']}")
    if row.get("exam_url"):
        description_lines.append(f"More info: {row['exam_url']}")
    if row.get("source_tab"):
        description_lines.append(f"Source sheet tab: {row['source_tab']}")

    body = {
        "summary": row["exam"] or "Exam event",
        "description": "\n".join(description_lines),
        "start": {"date": d.isoformat()},
        "end": {"date": end.isoformat()},
        "extendedProperties": {
            "private": {
                "sync_source": SYNC_SOURCE_TAG,
                "row_key": make_event_id(row["exam"], row["date"], row["event_type"]),
            }
        },
    }
    if row.get("exam_url"):
        body["source"] = {"title": "Exam details", "url": row["exam_url"]}
    return body


def build_calendar_service():
    creds = get_credentials()
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def list_existing_synced_event_ids(service, calendar_id: str) -> set[str]:
    """IDs of events on the calendar previously created by this script."""
    ids: set[str] = set()
    page_token = None
    while True:
        resp = (
            service.events()
            .list(
                calendarId=calendar_id,
                privateExtendedProperty=f"sync_source={SYNC_SOURCE_TAG}",
                showDeleted=False,
                singleEvents=True,
                pageToken=page_token,
                maxResults=2500,
            )
            .execute()
        )
        for item in resp.get("items", []):
            ids.add(item["id"])
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return ids


def upsert_event(service, calendar_id: str, event_id: str, body: dict) -> str:
    """Insert with a fixed ID; if it already exists, update in place instead."""
    body = dict(body)
    body["id"] = event_id
    try:
        service.events().insert(calendarId=calendar_id, body=body).execute()
        return "created"
    except HttpError as e:
        if e.resp.status == 409:
            service.events().update(
                calendarId=calendar_id, eventId=event_id, body=body
            ).execute()
            return "updated"
        raise


def delete_event(service, calendar_id: str, event_id: str) -> None:
    try:
        service.events().delete(calendarId=calendar_id, eventId=event_id).execute()
    except HttpError as e:
        if e.resp.status != 410:  # 410 = already gone, treat as success
            raise


def run_sync() -> SyncStats:
    stats = SyncStats()

    calendar_id = validate_and_clean_calendar_id(os.environ.get("GOOGLE_CALENDAR_ID", ""))

    tab_name = os.environ.get("SHEET_TAB_NAME") or None
    delete_removed = os.environ.get("DELETE_REMOVED_EVENTS", "false").strip().lower() == "true"

    log.info("Reading rows%s from the sheet …", f" from tab '{tab_name}'" if tab_name else " (all tabs)")
    rows = read_all_rows(tab_name=tab_name)
    log.info("Read %d row(s).", len(rows))

    service = build_calendar_service()

    desired_ids: set[str] = set()

    for row in rows:
        d = parse_row_date(row["date"])
        if d is None:
            log.warning("Skipping row with missing/invalid date: exam=%r date=%r", row.get("exam"), row.get("date"))
            stats.skipped += 1
            continue
        if not row.get("exam"):
            log.warning("Skipping row with empty exam name: %r", row)
            stats.skipped += 1
            continue

        event_id = make_event_id(row["exam"], row["date"], row["event_type"])
        desired_ids.add(event_id)
        body = row_to_event_body(row)

        try:
            action = upsert_event(service, calendar_id, event_id, body)
            if action == "created":
                stats.created += 1
                log.info("Created: %s (%s)", row["exam"], row["date"])
            else:
                stats.updated += 1
                log.info("Updated: %s (%s)", row["exam"], row["date"])
        except HttpError as e:
            stats.failed += 1
            log.error("Failed to sync '%s' (%s): %s", row["exam"], row["date"], e)

    if delete_removed:
        log.info("DELETE_REMOVED_EVENTS is enabled — checking for stale events …")
        existing_ids = list_existing_synced_event_ids(service, calendar_id)
        for event_id in existing_ids - desired_ids:
            try:
                delete_event(service, calendar_id, event_id)
                stats.deleted += 1
                log.info("Deleted stale event: %s", event_id)
            except HttpError as e:
                stats.failed += 1
                log.error("Failed to delete stale event %s: %s", event_id, e)
    else:
        log.info("DELETE_REMOVED_EVENTS is disabled — leaving stale events untouched.")

    return stats


def main() -> None:
    try:
        stats = run_sync()
    except RuntimeError as e:
        log.error("Configuration error: %s", e)
        sys.exit(1)
    except Exception as e:  # noqa: BLE001 — top-level guard
        log.error("Unexpected error during sync: %s", e, exc_info=True)
        sys.exit(1)

    log.info("Sync complete: %s", stats.summary())
    if stats.failed > 0:
        log.error("%d row(s)/event(s) failed during sync.", stats.failed)
        sys.exit(2)
    sys.exit(0)


if __name__ == "__main__":
    main()
