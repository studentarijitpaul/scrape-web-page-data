"""
calendar_sync.py

Reads exam rows from Google Sheets and synchronizes them to Google Calendar.

Features:
- Deterministic Calendar event IDs
- No insert-then-update pattern
- Existing events are loaded once
- Creates only missing events
- Updates only changed events
- Exponential backoff for Google API rate limits
- Handles 403 rateLimitExceeded
- Handles 429 Too Many Requests
- Handles 5xx Google API errors
- Optional deletion of removed events
- Safe for GitHub Actions

Required environment variables:
    GOOGLE_SERVICE_ACCOUNT_JSON
    GOOGLE_SHEET_ID
    GOOGLE_CALENDAR_ID

Optional:
    SHEET_TAB_NAME
    SYNC_TIMEZONE
    DELETE_REMOVED_EVENTS

Exit codes:
    0 = success
    1 = configuration/authentication error
    2 = one or more rows failed
"""

from __future__ import annotations

import hashlib
import logging
import os
import random
import re
import sys
import time
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

# Google Calendar custom event IDs:
# Only lowercase a-v and 0-9 are allowed.
EVENT_ID_PREFIX = "evt"

# Number of Calendar API retries after rate-limit/server errors.
MAX_RETRIES = 6

# Base delay for exponential backoff.
BACKOFF_BASE_SECONDS = 2

# Maximum backoff delay.
MAX_BACKOFF_SECONDS = 60

# Small delay between successful write operations.
# This is intentionally conservative to reduce rate-limit pressure.
WRITE_DELAY_MIN = 0.35
WRITE_DELAY_MAX = 0.75


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
# STATISTICS
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
# EVENT ID
# ============================================================

def make_event_id(
    exam: str,
    date_str: str,
    event_type: str,
) -> str:
    """
    Generate a deterministic Google Calendar event ID.

    Same:
        exam + date + event_type

    will always produce the same event ID.
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

    # Google Calendar custom event IDs must match:
    # [a-v0-9]{5,1024}
    if not re.fullmatch(
        r"[a-v0-9]{5,1024}",
        event_id,
    ):
        raise ValueError(
            f"Generated invalid Google Calendar event ID: "
            f"{event_id!r}"
        )

    return event_id


# ============================================================
# DATE PARSING
# ============================================================

def parse_row_date(
    date_str: str,
) -> Optional[date]:
    """
    Parse YYYY-MM-DD.

    Returns None for invalid dates.
    """

    date_str = (date_str or "").strip()

    if not date_str:
        return None

    try:
        return datetime.strptime(
            date_str,
            "%Y-%m-%d",
        ).date()

    except ValueError:
        return None


# ============================================================
# SHEET ROW -> CALENDAR EVENT
# ============================================================

def row_to_event_body(row: dict) -> dict:
    """
    Convert one Google Sheet row into a Calendar event.
    """

    d = parse_row_date(row.get("date", ""))

    if d is None:
        raise ValueError(
            f"Invalid date: {row.get('date')!r}"
        )

    end = d + timedelta(days=1)

    description_lines = []

    if row.get("event"):
        description_lines.append(
            str(row["event"]).strip()
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
        row.get("event_type", ""),
    )

    body = {
        "summary": row["exam"] or "Exam event",

        "description": "\n".join(
            description_lines
        ),

        "start": {
            "date": d.isoformat(),
        },

        "end": {
            "date": end.isoformat(),
        },

        "extendedProperties": {
            "private": {
                "sync_source": SYNC_SOURCE_TAG,
                "row_key": event_id,
            }
        },
    }

    if row.get("exam_url"):
        body["source"] = {
            "title": "Exam details",
            "url": row["exam_url"],
        }

    return body


# ============================================================
# GOOGLE CALENDAR SERVICE
# ============================================================

def build_calendar_service():
    """
    Build authenticated Google Calendar API service.
    """

    creds = get_credentials()

    return build(
        "calendar",
        "v3",
        credentials=creds,
        cache_discovery=False,
    )


# ============================================================
# GOOGLE API ERROR HELPERS
# ============================================================

def get_error_reason(error: HttpError) -> str:
    """
    Extract Google's error reason when possible.
    """

    try:
        content = error.content.decode(
            "utf-8",
            errors="ignore",
        )

        content_lower = content.lower()

        if "ratelimitexceeded" in content_lower:
            return "rateLimitExceeded"

        if "quotaexceeded" in content_lower:
            return "quotaExceeded"

        if "backenderror" in content_lower:
            return "backendError"

    except Exception:
        pass

    return ""


def is_retryable_error(error: HttpError) -> bool:
    """
    Determine whether a Google API error should be retried.
    """

    status = getattr(
        error.resp,
        "status",
        None,
    )

    reason = get_error_reason(error)

    # Google recommends exponential backoff for
    # rateLimitExceeded and similar temporary failures.
    if reason in {
        "rateLimitExceeded",
        "backendError",
    }:
        return True

    if status in {
        429,  # Too Many Requests
        500,
        502,
        503,
        504,
    }:
        return True

    return False


# ============================================================
# EXPONENTIAL BACKOFF
# ============================================================

def execute_with_retry(
    request_factory,
    operation_name: str,
):
    """
    Execute a Google API request with exponential backoff.

    request_factory must be a function that returns
    a fresh Google API request object.
    """

    for attempt in range(MAX_RETRIES + 1):

        try:
            return request_factory().execute()

        except HttpError as error:

            if not is_retryable_error(error):

                log.error(
                    "%s failed with non-retryable error: %s",
                    operation_name,
                    error,
                )

                raise

            if attempt >= MAX_RETRIES:

                log.error(
                    "%s failed after %d retries: %s",
                    operation_name,
                    MAX_RETRIES,
                    error,
                )

                raise

            # Exponential backoff:
            #
            # attempt 0 -> 2 sec
            # attempt 1 -> 4 sec
            # attempt 2 -> 8 sec
            # attempt 3 -> 16 sec
            # etc.
            delay = min(
                MAX_BACKOFF_SECONDS,
                BACKOFF_BASE_SECONDS * (
                    2 ** attempt
                ),
            )

            # Add jitter so repeated GitHub Actions
            # runs don't retry at exactly the same time.
            delay += random.uniform(
                0,
                1.5,
            )

            status = getattr(
                error.resp,
                "status",
                "unknown",
            )

            reason = get_error_reason(
                error
            )

            log.warning(
                "%s hit temporary Google API error "
                "(HTTP %s, %s). "
                "Retrying in %.1f seconds "
                "(attempt %d/%d)...",
                operation_name,
                status,
                reason or "temporary_error",
                delay,
                attempt + 1,
                MAX_RETRIES,
            )

            time.sleep(delay)

    raise RuntimeError(
        f"Unexpected retry loop termination: "
        f"{operation_name}"
    )


# ============================================================
# SMALL DELAY BETWEEN WRITES
# ============================================================

def throttle_calendar_write():
    """
    Add a small randomized delay between writes.

    This reduces the chance of hitting per-user/per-calendar
    request-rate limits.
    """

    delay = random.uniform(
        WRITE_DELAY_MIN,
        WRITE_DELAY_MAX,
    )

    time.sleep(delay)


# ============================================================
# LOAD EXISTING SYNC EVENTS
# ============================================================

def list_existing_synced_events(
    service,
    calendar_id: str,
) -> dict[str, dict]:
    """
    Load all events previously created by this script.

    Returns:

        {
            event_id: event_object
        }

    This is the important optimization.

    The old implementation attempted:

        insert()
        -> 409
        -> update()

    for every existing event.

    This implementation first loads existing events once,
    then knows whether to insert or update.
    """

    events_by_id: dict[str, dict] = {}

    page_token = None

    while True:

        def request():
            return (
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
            )

        response = execute_with_retry(
            request,
            "List existing synced Calendar events",
        )

        for item in response.get(
            "items",
            [],
        ):

            event_id = item.get("id")

            if event_id:
                events_by_id[event_id] = item

        page_token = response.get(
            "nextPageToken"
        )

        if not page_token:
            break

    log.info(
        "Found %d existing synced Calendar event(s).",
        len(events_by_id),
    )

    return events_by_id


# ============================================================
# EVENT COMPARISON
# ============================================================

def normalize_event_for_comparison(
    event: dict,
) -> dict:
    """
    Keep only fields controlled by this script.

    This prevents unnecessary Calendar updates caused by
    unrelated Google-managed fields such as:
        etag
        updated
        htmlLink
        creator
        organizer
    """

    return {
        "summary": event.get(
            "summary",
            "",
        ),
        "description": event.get(
            "description",
            "",
        ),
        "start": event.get(
            "start",
            {},
        ),
        "end": event.get(
            "end",
            {},
        ),
        "source": event.get(
            "source",
            {},
        ),
        "extendedProperties": {
            "private": event.get(
                "extendedProperties",
                {},
            ).get(
                "private",
                {},
            )
        },
    }


def event_needs_update(
    existing_event: dict,
    desired_body: dict,
) -> bool:
    """
    Return True only if our controlled event data changed.
    """

    existing_normalized = (
        normalize_event_for_comparison(
            existing_event
        )
    )

    desired_normalized = (
        normalize_event_for_comparison(
            desired_body
        )
    )

    return (
        existing_normalized
        != desired_normalized
    )


# ============================================================
# INSERT EVENT
# ============================================================

def create_event(
    service,
    calendar_id: str,
    event_id: str,
    body: dict,
):
    """
    Create a new event using deterministic ID.
    """

    request_body = dict(body)

    request_body["id"] = event_id

    throttle_calendar_write()

    def request():
        return (
            service.events()
            .insert(
                calendarId=calendar_id,
                body=request_body,
            )
        )

    return execute_with_retry(
        request,
        f"Create event {event_id}",
    )


# ============================================================
# UPDATE EVENT
# ============================================================

def update_event(
    service,
    calendar_id: str,
    event_id: str,
    body: dict,
):
    """
    Update an existing event.
    """

    request_body = dict(body)

    request_body["id"] = event_id

    throttle_calendar_write()

    def request():
        return (
            service.events()
            .update(
                calendarId=calendar_id,
                eventId=event_id,
                body=request_body,
            )
        )

    return execute_with_retry(
        request,
        f"Update event {event_id}",
    )


# ============================================================
# DELETE EVENT
# ============================================================

def delete_event(
    service,
    calendar_id: str,
    event_id: str,
) -> None:
    """
    Delete an event.

    410 means it is already gone, so we treat it as success.
    """

    throttle_calendar_write()

    def request():
        return (
            service.events()
            .delete(
                calendarId=calendar_id,
                eventId=event_id,
            )
        )

    try:

        execute_with_retry(
            request,
            f"Delete event {event_id}",
        )

    except HttpError as error:

        status = getattr(
            error.resp,
            "status",
            None,
        )

        if status == 410:
            return

        raise


# ============================================================
# MAIN SYNC
# ============================================================

def run_sync() -> SyncStats:

    stats = SyncStats()

    # --------------------------------------------------------
    # Calendar ID
    # --------------------------------------------------------

    calendar_id = (
        os.environ.get(
            "GOOGLE_CALENDAR_ID"
        )
        or ""
    ).strip()

    if not calendar_id:

        raise RuntimeError(
            "GOOGLE_CALENDAR_ID environment "
            "variable is not set."
        )

    # --------------------------------------------------------
    # Optional configuration
    # --------------------------------------------------------

    tab_name = (
        os.environ.get(
            "SHEET_TAB_NAME"
        )
        or None
    )

    delete_removed = (
        os.environ.get(
            "DELETE_REMOVED_EVENTS",
            "false",
        )
        .strip()
        .lower()
        == "true"
    )

    # --------------------------------------------------------
    # Read Google Sheet
    # --------------------------------------------------------

    if tab_name:

        log.info(
            "Reading rows from tab '%s'...",
            tab_name,
        )

    else:

        log.info(
            "Reading rows from all tabs..."
        )

    rows = read_all_rows(
        tab_name=tab_name
    )

    log.info(
        "Read %d row(s).",
        len(rows),
    )

    if not rows:

        log.warning(
            "No rows found in Google Sheet."
        )

        return stats

    # --------------------------------------------------------
    # Build Calendar API service
    # --------------------------------------------------------

    service = build_calendar_service()

    # --------------------------------------------------------
    # IMPORTANT:
    #
    # Load existing synced events ONCE.
    # --------------------------------------------------------

    log.info(
        "Loading existing synced Calendar events..."
    )

    existing_events = (
        list_existing_synced_events(
            service,
            calendar_id,
        )
    )

    log.info(
        "Calendar contains %d existing "
        "event(s) created by this sync.",
        len(existing_events),
    )

    # --------------------------------------------------------
    # IDs that should remain after sync
    # --------------------------------------------------------

    desired_ids: set[str] = set()

    # --------------------------------------------------------
    # Process Sheet rows
    # --------------------------------------------------------

    for row_number, row in enumerate(
        rows,
        start=1,
    ):

        exam = str(
            row.get("exam") or ""
        ).strip()

        date_str = str(
            row.get("date") or ""
        ).strip()

        event_type = str(
            row.get("event_type") or ""
        ).strip()

        # ----------------------------------------------------
        # Validate date
        # ----------------------------------------------------

        d = parse_row_date(
            date_str
        )

        if d is None:

            log.warning(
                "Skipping row %d: invalid date=%r",
                row_number,
                date_str,
            )

            stats.skipped += 1

            continue

        # ----------------------------------------------------
        # Validate exam name
        # ----------------------------------------------------

        if not exam:

            log.warning(
                "Skipping row %d: empty exam name.",
                row_number,
            )

            stats.skipped += 1

            continue

        # ----------------------------------------------------
        # Deterministic event ID
        # ----------------------------------------------------

        try:

            event_id = make_event_id(
                exam,
                date_str,
                event_type,
            )

        except Exception as error:

            log.error(
                "Could not generate event ID "
                "for row %d: %s",
                row_number,
                error,
            )

            stats.failed += 1

            continue

        desired_ids.add(
            event_id
        )

        # ----------------------------------------------------
        # Build desired Calendar body
        # ----------------------------------------------------

        try:

            body = row_to_event_body(
                row
            )

        except Exception as error:

            log.error(
                "Failed to build Calendar event "
                "for '%s' (%s): %s",
                exam,
                date_str,
                error,
            )

            stats.failed += 1

            continue

        # ----------------------------------------------------
        # CREATE or UPDATE
        # ----------------------------------------------------

        try:

            existing_event = (
                existing_events.get(
                    event_id
                )
            )

            # ------------------------------------------------
            # Event doesn't exist -> CREATE
            # ------------------------------------------------

            if existing_event is None:

                create_event(
                    service,
                    calendar_id,
                    event_id,
                    body,
                )

                stats.created += 1

                log.info(
                    "Created: %s (%s)",
                    exam,
                    date_str,
                )

            # ------------------------------------------------
            # Event exists and changed -> UPDATE
            # ------------------------------------------------

            elif event_needs_update(
                existing_event,
                body,
            ):

                update_event(
                    service,
                    calendar_id,
                    event_id,
                    body,
                )

                stats.updated += 1

                log.info(
                    "Updated: %s (%s)",
                    exam,
                    date_str,
                )

            # ------------------------------------------------
            # Event exists and is unchanged
            # ------------------------------------------------

            else:

                stats.skipped += 1

                log.info(
                    "Unchanged: %s (%s)",
                    exam,
                    date_str,
                )

        except HttpError as error:

            stats.failed += 1

            log.error(
                "Failed to sync '%s' (%s): %s",
                exam,
                date_str,
                error,
            )

        except Exception as error:

            stats.failed += 1

            log.error(
                "Unexpected error syncing "
                "'%s' (%s): %s",
                exam,
                date_str,
                error,
                exc_info=True,
            )

    # ========================================================
    # DELETE REMOVED EVENTS
    # ========================================================

    if delete_removed:

        log.info(
            "DELETE_REMOVED_EVENTS is enabled."
        )

        log.info(
            "Checking for stale Calendar events..."
        )

        stale_ids = (
            set(existing_events.keys())
            - desired_ids
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

            except Exception as error:

                stats.failed += 1

                log.error(
                    "Unexpected error deleting "
                    "event %s: %s",
                    event_id,
                    error,
                    exc_info=True,
                )

    else:

        log.info(
            "DELETE_REMOVED_EVENTS is disabled "
            "— leaving stale events untouched."
        )

    return stats


# ============================================================
# MAIN
# ============================================================

def main() -> None:

    try:

        stats = run_sync()

    except RuntimeError as error:

        log.error(
            "Configuration error: %s",
            error,
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
            "%d row(s)/event(s) failed during sync.",
            stats.failed,
        )

        sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    main()
    
