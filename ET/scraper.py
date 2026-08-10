from __future__ import annotations

import hashlib
import logging
import os
import re
import sys
import time
import random

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

# Google Calendar custom event IDs may contain:
# a-v and 0-9
EVENT_ID_PREFIX = "evt"

# Number of retries for rate-limit/server errors
MAX_RETRIES = 7

# Base retry delay.
# Actual delay uses exponential backoff + jitter.
BASE_RETRY_DELAY = 2

# Maximum retry delay
MAX_RETRY_DELAY = 60

# Small delay between successful API operations.
# This helps prevent rate limiting.
MIN_OPERATION_DELAY = 0.8
MAX_OPERATION_DELAY = 1.8

# Calendar name to prefer when automatically detecting
TARGET_CALENDAR_NAME = "eng"


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
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
# RATE LIMIT / RETRY HELPERS
# ============================================================

def is_retryable_http_error(error: HttpError) -> bool:
    """
    Determine whether a Google API error should be retried.

    Retry:
        403 rateLimitExceeded
        403 userRateLimitExceeded
        403 quotaExceeded
        429 Too Many Requests
        500
        502
        503
        504

    Do not retry ordinary errors such as:
        400
        401
        404
        409
        etc.
    """

    try:
        status = int(error.resp.status)
    except Exception:
        return False

    if status in (429, 500, 502, 503, 504):
        return True

    if status != 403:
        return False

    try:
        content = error.content.decode("utf-8", errors="ignore").lower()
    except Exception:
        content = str(error).lower()

    retry_reasons = (
        "ratelimitexceeded",
        "userratelimitexceeded",
        "quotaexceeded",
        "backenderror",
    )

    return any(
        reason in content
        for reason in retry_reasons
    )


def retry_delay(attempt: int) -> float:
    """
    Exponential backoff with jitter.

    attempt=1 -> roughly 2 sec
    attempt=2 -> roughly 4 sec
    attempt=3 -> roughly 8 sec
    ...
    """

    delay = min(
        BASE_RETRY_DELAY * (2 ** (attempt - 1)),
        MAX_RETRY_DELAY,
    )

    # Add random jitter so repeated jobs do not
    # all hit the API at exactly the same time.
    jitter = random.uniform(0.5, 1.5)

    return delay * jitter


def execute_with_retry(
    request_factory,
    operation_name: str,
):
    """
    Execute a Google API request with exponential backoff.

    request_factory must be a function that returns
    a fresh Google API request object.

    Example:

        execute_with_retry(
            lambda: service.events().get(...),
            "get event"
        )
    """

    for attempt in range(1, MAX_RETRIES + 1):

        try:

            result = request_factory().execute()

            # Small delay after every successful operation.
            time.sleep(
                random.uniform(
                    MIN_OPERATION_DELAY,
                    MAX_OPERATION_DELAY,
                )
            )

            return result

        except HttpError as error:

            if not is_retryable_http_error(error):

                raise

            if attempt >= MAX_RETRIES:

                log.error(
                    "Google API operation failed after %d retries: %s",
                    MAX_RETRIES,
                    operation_name,
                )

                raise

            delay = retry_delay(attempt)

            log.warning(
                "Google API rate/server limit during '%s'. "
                "Retry %d/%d in %.1f seconds.",
                operation_name,
                attempt,
                MAX_RETRIES,
                delay,
            )

            time.sleep(delay)


# ============================================================
# EVENT ID
# ============================================================

def make_event_id(
    exam: str,
    date_str: str,
    event_type: str,
) -> str:
    """
    Generate a deterministic Google Calendar event ID.

    The same:
        exam + date + event type

    will always generate the same event ID.

    This allows the script to UPDATE an existing event
    instead of creating duplicates.

    Google Calendar custom IDs must contain only:
        a-v
        0-9
    """

    key = (
        f"{exam.strip().lower()}|"
        f"{date_str.strip()}|"
        f"{event_type.strip().lower()}"
    )

    digest = hashlib.sha256(
        key.encode("utf-8")
    ).hexdigest()

    event_id = f"{EVENT_ID_PREFIX}{digest}"

    # Google Calendar allowed characters:
    # a-v and 0-9
    if not re.fullmatch(
        r"[a-v0-9]{5,1024}",
        event_id,
    ):
        raise ValueError(
            f"Generated invalid Google Calendar event ID: "
            f"{event_id}"
        )

    return event_id


# ============================================================
# DATE PARSER
# ============================================================

def parse_row_date(
    date_str: str,
) -> Optional[date]:
    """
    Convert supported date strings into date objects.

    Primary format:
        YYYY-MM-DD

    Also supports common formats just in case.
    """

    date_str = (
        date_str or ""
    ).strip()

    if not date_str:
        return None

    formats = (
        "%Y-%m-%d",
        "%d-%m-%Y",
        "%d/%m/%Y",
        "%Y/%m/%d",
        "%d %B %Y",
        "%d %b %Y",
    )

    for fmt in formats:

        try:

            return datetime.strptime(
                date_str,
                fmt,
            ).date()

        except ValueError:
            continue

    return None


# ============================================================
# GOOGLE CALENDAR SERVICE
# ============================================================

def build_calendar_service():

    """
    Build Google Calendar API service using the
    credentials provided by google_sheets.py.
    """

    creds = get_credentials()

    if creds is None:

        raise RuntimeError(
            "Google credentials could not be loaded."
        )

    service = build(
        "calendar",
        "v3",
        credentials=creds,
        cache_discovery=False,
    )

    return service


# ============================================================
# AUTOMATIC CALENDAR DETECTION
# ============================================================

def get_calendar_id(
    service,
) -> str:
    """
    Get Google Calendar ID.

    Priority:

    1. GOOGLE_CALENDAR_ID environment variable
    2. Calendar named 'eng'
    3. First writable calendar

    This is important because the previous working
    version automatically detected the calendar.
    """

    # --------------------------------------------------------
    # 1. Environment variable
    # --------------------------------------------------------

    calendar_id = os.getenv(
        "GOOGLE_CALENDAR_ID",
        "",
    ).strip()

    if calendar_id:

        log.info(
            "Using GOOGLE_CALENDAR_ID from environment."
        )

        log.info(
            "Calendar ID detected: %s",
            calendar_id,
        )

        return calendar_id

    # --------------------------------------------------------
    # 2. Automatically list calendars
    # --------------------------------------------------------

    log.info(
        "GOOGLE_CALENDAR_ID not configured."
    )

    log.info(
        "Attempting automatic calendar detection..."
    )

    calendars = []

    page_token = None

    while True:

        response = execute_with_retry(
            lambda: service.calendarList().list(
                pageToken=page_token,
                minAccessRole="writer",
            ),
            "list calendars",
        )

        calendars.extend(
            response.get(
                "items",
                [],
            )
        )

        page_token = response.get(
            "nextPageToken"
        )

        if not page_token:
            break

    if not calendars:

        raise RuntimeError(
            "No writable Google Calendars were found. "
            "Make sure the service account has access "
            "to the calendar."
        )

    # --------------------------------------------------------
    # Log calendars
    # --------------------------------------------------------

    log.info(
        "Found %d writable calendar(s).",
        len(calendars),
    )

    for calendar in calendars:

        log.info(
            "Available calendar: %s | ID: %s",
            calendar.get(
                "summary",
                "Unnamed",
            ),
            calendar.get(
                "id",
                "",
            ),
        )

    # --------------------------------------------------------
    # 3. Prefer "eng"
    # --------------------------------------------------------

    for calendar in calendars:

        name = (
            calendar.get(
                "summary",
                "",
            )
            .strip()
            .lower()
        )

        if name == TARGET_CALENDAR_NAME.lower():

            calendar_id = calendar.get("id")

            if calendar_id:

                log.info(
                    "Calendar ID detected: %s",
                    calendar_id,
                )

                return calendar_id

    # --------------------------------------------------------
    # 4. Fallback to first writable calendar
    # --------------------------------------------------------

    for calendar in calendars:

        calendar_id = calendar.get("id")

        if calendar_id:

            log.warning(
                "Calendar '%s' was not found.",
                TARGET_CALENDAR_NAME,
            )

            log.info(
                "Using first writable calendar: %s",
                calendar.get(
                    "summary",
                    "Unnamed",
                ),
            )

            log.info(
                "Calendar ID detected: %s",
                calendar_id,
            )

            return calendar_id

    raise RuntimeError(
        "Could not determine a valid Google Calendar ID."
    )


# ============================================================
# TEST CALENDAR ACCESS
# ============================================================

def test_calendar_access(
    service,
    calendar_id: str,
):
    """
    Test whether the service account can access
    the specified Google Calendar.
    """

    log.info(
        "Testing Google Calendar access..."
    )

    try:

        calendar = execute_with_retry(
            lambda: service.calendars().get(
                calendarId=calendar_id,
            ),
            "test calendar access",
        )

    except HttpError as error:

        raise RuntimeError(
            f"Unable to access Google Calendar: {error}"
        )

    log.info(
        "Google Calendar connection successful!"
    )

    log.info(
        "Calendar name: %s",
        calendar.get(
            "summary",
            "Unknown",
        ),
    )

    log.info(
        "Calendar timezone: %s",
        calendar.get(
            "timeZone",
            DEFAULT_TIMEZONE,
        ),
    )


# ============================================================
# ROW -> GOOGLE CALENDAR EVENT
# ============================================================

def row_to_event_body(
    row: dict,
) -> dict:
    """
    Convert a Google Sheet row into a Google Calendar
    all-day event.
    """

    date_str = str(
        row.get(
            "date",
            "",
        )
    ).strip()

    d = parse_row_date(
        date_str
    )

    if d is None:

        raise ValueError(
            f"Invalid date: {date_str}"
        )

    # Google Calendar all-day event end date
    # is EXCLUSIVE.
    end = d + timedelta(
        days=1
    )

    exam = str(
        row.get(
            "exam",
            "",
        )
    ).strip()

    event_type = str(
        row.get(
            "Event",
            row.get(
                "event",
                "",
            ),
        )
    ).strip()

    label = str(
        row.get(
            "label",
            "",
        )
    ).strip()

    description_lines = []

    if exam:
        description_lines.append(
            f"Exam: {exam}"
        )

    if label:
        description_lines.append(
            f"Label: {label}"
        )

    if event_type:
        description_lines.append(
            f"Event: {event_type}"
        )

    # Add remaining columns to description.
    used_fields = {
        "date",
        "exam",
        "label",
        "Event",
        "event",
    }

    for key, value in row.items():

        if key in used_fields:
            continue

        if value is None:
            continue

        value = str(value).strip()

        if not value:
            continue

        description_lines.append(
            f"{key}: {value}"
        )

    description_lines.append(
        f"Source: {SYNC_SOURCE_TAG}"
    )

    description = "\n".join(
        description_lines
    )

    summary_parts = []

    if exam:
        summary_parts.append(exam)

    if event_type:
        summary_parts.append(
            event_type
        )

    if label:
        summary_parts.append(
            label
        )

    summary = " - ".join(
        summary_parts
    )

    if not summary:
        summary = "Exam Calendar Event"

    body = {
        "summary": summary,

        "description": description,

        "start": {
            "date": d.isoformat(),
        },

        "end": {
            "date": end.isoformat(),
        },

        "extendedProperties": {
            "private": {
                "sync_source": SYNC_SOURCE_TAG,
            }
        },
    }

    return body


# ============================================================
# CREATE / UPDATE EVENT
# ============================================================

def upsert_event(
    service,
    calendar_id: str,
    event_id: str,
    body: dict,
) -> str:
    """
    Create an event.

    If the event already exists, update it.

    Returns:
        "created"
        "updated"
    """

    # --------------------------------------------------------
    # First try to UPDATE the deterministic event ID.
    # --------------------------------------------------------

    try:

        execute_with_retry(
            lambda: service.events().update(
                calendarId=calendar_id,
                eventId=event_id,
                body=body,
            ),
            "update event",
        )

        return "updated"

    except HttpError as error:

        # 404 means the event does not exist.
        try:
            status = int(
                error.resp.status
            )
        except Exception:
            status = 0

        if status != 404:

            # 409 can happen if another operation created
            # the same deterministic ID.
            if status == 409:

                try:

                    execute_with_retry(
                        lambda: service.events().update(
                            calendarId=calendar_id,
                            eventId=event_id,
                            body=body,
                        ),
                        "resolve conflicting event",
                    )

                    return "updated"

                except HttpError:
                    raise

            raise

    # --------------------------------------------------------
    # Event does not exist -> INSERT
    # --------------------------------------------------------

    try:

        execute_with_retry(
            lambda: service.events().insert(
                calendarId=calendar_id,
                body=body,
                sendUpdates="none",
            ),
            "create event",
        )

        return "created"

    except HttpError as error:

        # If another process created it between GET/INSERT,
        # try update once.
        try:
            status = int(
                error.resp.status
            )
        except Exception:
            status = 0

        if status == 409:

            execute_with_retry(
                lambda: service.events().update(
                    calendarId=calendar_id,
                    eventId=event_id,
                    body=body,
                ),
                "update after conflict",
            )

            return "updated"

        raise


# ============================================================
# DELETE EVENT
# ============================================================

def delete_event(
    service,
    calendar_id: str,
    event_id: str,
):
    """
    Delete a Google Calendar event.

    404 means the event is already gone,
    so it is ignored.
    """

    try:

        execute_with_retry(
            lambda: service.events().delete(
                calendarId=calendar_id,
                eventId=event_id,
            ),
            "delete event",
        )

    except HttpError as error:

        try:
            status = int(
                error.resp.status
            )
        except Exception:
            status = 0

        # Already deleted.
        if status == 404:
            return

        raise


# ============================================================
# FIND EXISTING SYNC EVENTS
# ============================================================

def list_existing_synced_event_ids(
    service,
    calendar_id: str,
) -> set[str]:
    """
    Find all events previously created by this
    synchronization script.

    Uses extendedProperties instead of searching the
    description manually.
    """

    ids = set()

    page_token = None

    while True:

        response = execute_with_retry(
            lambda: service.events().list(
                calendarId=calendar_id,
                privateExtendedProperty=(
                    f"sync_source={SYNC_SOURCE_TAG}"
                ),
                showDeleted=False,
                singleEvents=True,
                maxResults=2500,
                pageToken=page_token,
            ),
            "list synced events",
        )

        for event in response.get(
            "items",
            [],
        ):

            event_id = event.get("id")

            if event_id:
                ids.add(event_id)

        page_token = response.get(
            "nextPageToken"
        )

        if not page_token:
            break

    return ids


# ============================================================
# MAIN SYNC
# ============================================================

def run_sync():

    # --------------------------------------------------------
    # Read Google Sheet
    # --------------------------------------------------------

    log.info(
        "Reading rows (all tabs) from Google Sheet..."
    )

    rows = read_all_rows()

    log.info(
        "Read %d row(s).",
        len(rows),
    )

    if not rows:

        log.warning(
            "No rows found in Google Sheet."
        )

        return SyncStats()

    # --------------------------------------------------------
    # Build Calendar service
    # --------------------------------------------------------

    service = build_calendar_service()

    # --------------------------------------------------------
    # Detect Calendar automatically
    # --------------------------------------------------------

    calendar_id = get_calendar_id(
        service
    )

    # --------------------------------------------------------
    # Test Calendar
    # --------------------------------------------------------

    test_calendar_access(
        service,
        calendar_id,
    )

    # --------------------------------------------------------
    # Configuration
    # --------------------------------------------------------

    delete_removed = (
        os.getenv(
            "DELETE_REMOVED_EVENTS",
            "false",
        )
        .strip()
        .lower()
        in (
            "1",
            "true",
            "yes",
            "on",
        )
    )

    if delete_removed:

        log.info(
            "DELETE_REMOVED_EVENTS enabled."
        )

    else:

        log.info(
            "DELETE_REMOVED_EVENTS disabled."
        )

    # --------------------------------------------------------
    # Statistics
    # --------------------------------------------------------

    stats = SyncStats()

    desired_ids = set()

    # --------------------------------------------------------
    # Process rows
    # --------------------------------------------------------

    for index, row in enumerate(
        rows,
        start=1,
    ):

        exam = str(
            row.get(
                "exam",
                "",
            )
        ).strip()

        date_str = str(
            row.get(
                "date",
                "",
            )
        ).strip()

        event_type = str(
            row.get(
                "Event",
                row.get(
                    "event",
                    "",
                ),
            )
        ).strip()

        # ----------------------------------------------------
        # Validate
        # ----------------------------------------------------

        if not exam:

            stats.skipped += 1

            log.warning(
                "Skipping row %d: exam is empty.",
                index,
            )

            continue

        if not date_str:

            stats.skipped += 1

            log.warning(
                "Skipping row %d: date is empty.",
                index,
            )

            continue

        parsed_date = parse_row_date(
            date_str
        )

        if parsed_date is None:

            stats.skipped += 1

            log.warning(
                "Skipping row %d: invalid date '%s'.",
                index,
                date_str,
            )

            continue

        # Normalize date for event ID.
        normalized_date = (
            parsed_date.isoformat()
        )

        # ----------------------------------------------------
        # Generate deterministic ID
        # ----------------------------------------------------

        try:

            event_id = make_event_id(
                exam,
                normalized_date,
                event_type,
            )

        except Exception as error:

            stats.failed += 1

            log.error(
                "Could not generate event ID for "
                "'%s' (%s): %s",
                exam,
                date_str,
                error,
            )

            continue

        desired_ids.add(
            event_id
        )

        # ----------------------------------------------------
        # Build event body
        # ----------------------------------------------------

        try:

            body = row_to_event_body(
                row
            )

        except Exception as error:

            stats.failed += 1

            log.error(
                "Failed to build event for "
                "'%s' (%s): %s",
                exam,
                date_str,
                error,
            )

            continue

        # ----------------------------------------------------
        # Create / Update
        # ----------------------------------------------------

        try:

            action = upsert_event(
                service,
                calendar_id,
                event_id,
                body,
            )

            if action == "created":

                stats.created += 1

                log.info(
                    "Created: %s (%s)",
                    exam,
                    normalized_date,
                )

            elif action == "updated":

                stats.updated += 1

                log.info(
                    "Updated: %s (%s)",
                    exam,
                    normalized_date,
                )

        except HttpError as error:

            stats.failed += 1

            log.error(
                "Failed to sync '%s' (%s): %s",
                exam,
                normalized_date,
                error,
            )

        except Exception as error:

            stats.failed += 1

            log.error(
                "Unexpected error syncing "
                "'%s' (%s): %s",
                exam,
                normalized_date,
                error,
                exc_info=True,
            )

    # --------------------------------------------------------
    # Delete removed events
    # --------------------------------------------------------

    if delete_removed:

        log.info(
            "Finding previously synced events..."
        )

        try:

            existing_ids = (
                list_existing_synced_event_ids(
                    service,
                    calendar_id,
                )
            )

            stale_ids = (
                existing_ids - desired_ids
            )

            log.info(
                "Found %d stale event(s).",
                len(stale_ids),
            )

            for event_id in stale_ids:

                try:

                    delete_event(
                        service,
                        calendar_id,
                        event_id,
                    )

                    stats.deleted += 1

                    log.info(
                        "Deleted stale event: %s",
                        event_id,
                    )

                except HttpError as error:

                    stats.failed += 1

                    log.error(
                        "Failed to delete stale "
                        "event %s: %s",
                        event_id,
                        error,
                    )

        except HttpError as error:

            stats.failed += 1

            log.error(
                "Could not list existing synced events: %s",
                error,
            )

    # --------------------------------------------------------
    # Final statistics
    # --------------------------------------------------------

    return stats


# ============================================================
# ENTRY POINT
# ============================================================

def main():

    try:

        stats = run_sync()

    except RuntimeError as error:

        log.error(
            "Configuration error: %s",
            error,
        )

        sys.exit(1)

    except HttpError as error:

        log.error(
            "Google API error: %s",
            error,
            exc_info=True,
        )

        sys.exit(1)

    except Exception as error:

        log.error(
            "Unexpected error during sync: %s",
            error,
            exc_info=True,
        )

        sys.exit(1)

    log.info(
        "Sync complete: %s",
        stats.summary(),
    )

    if stats.failed > 0:

        log.error(
            "%d row(s)/event(s) failed.",
            stats.failed,
        )

        # We deliberately return exit code 2 only if
        # failures remain AFTER retrying.
        sys.exit(2)

    sys.exit(0)


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    main()
