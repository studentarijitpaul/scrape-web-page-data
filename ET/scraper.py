```python
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

EVENT_ID_PREFIX = "exam"

# Minimum delay between successful Calendar API operations.
# Increase this if Google continues returning rateLimitExceeded.
MIN_REQUEST_DELAY = float(
    os.environ.get("CALENDAR_REQUEST_DELAY", "0.8")
)

# Maximum number of retries for temporary/rate-limit errors.
MAX_RETRIES = int(
    os.environ.get("CALENDAR_MAX_RETRIES", "7")
)

# Initial exponential-backoff delay.
BACKOFF_INITIAL = float(
    os.environ.get("CALENDAR_BACKOFF_INITIAL", "2")
)

# Maximum backoff delay.
BACKOFF_MAX = float(
    os.environ.get("CALENDAR_BACKOFF_MAX", "60")
)

# After this many consecutive rate-limit errors,
# wait longer before continuing.
RATE_LIMIT_COOLDOWN = float(
    os.environ.get("CALENDAR_RATE_LIMIT_COOLDOWN", "30")
)

# Whether removed events should be deleted.
DELETE_REMOVED_EVENTS = (
    os.environ.get(
        "DELETE_REMOVED_EVENTS",
        "false"
    )
    .strip()
    .lower()
    == "true"
)


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
# STATS
# ============================================================

@dataclass
class SyncStats:

    created: int = 0
    updated: int = 0
    skipped: int = 0
    deleted: int = 0
    failed: int = 0
    retried: int = 0

    def summary(self) -> str:

        return (
            f"created={self.created} "
            f"updated={self.updated} "
            f"skipped={self.skipped} "
            f"deleted={self.deleted} "
            f"failed={self.failed} "
            f"retried={self.retried}"
        )


# ============================================================
# GLOBAL REQUEST THROTTLING
# ============================================================

_last_request_time = 0.0


def throttle():

    """
    Make sure Calendar API requests are not sent too quickly.
    """

    global _last_request_time

    now = time.monotonic()

    elapsed = now - _last_request_time

    if elapsed < MIN_REQUEST_DELAY:

        sleep_time = MIN_REQUEST_DELAY - elapsed

        # Small random jitter prevents perfectly regular bursts.
        sleep_time += random.uniform(0.05, 0.25)

        time.sleep(sleep_time)

    _last_request_time = time.monotonic()


def cooldown(seconds: float):

    log.warning(
        "Calendar API rate limit detected. "
        "Cooling down for %.1f seconds...",
        seconds
    )

    time.sleep(seconds)


# ============================================================
# CALENDAR ID
# ============================================================

def validate_and_clean_calendar_id(
    calendar_id: str
) -> str:

    """
    Clean and validate GOOGLE_CALENDAR_ID.
    """

    if not calendar_id:

        raise RuntimeError(
            "GOOGLE_CALENDAR_ID is not set."
        )

    cleaned = calendar_id.strip()

    # Remove surrounding quotes.
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
            "GOOGLE_CALENDAR_ID appears invalid."
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

    Same exam + date + event type
    always produces the same event ID.
    """

    key = (
        f"{exam.strip().lower()}|"
        f"{date_str.strip()}|"
        f"{event_type.strip().lower()}"
    )

    digest = hashlib.md5(
        key.encode("utf-8")
    ).hexdigest()

    return f"{EVENT_ID_PREFIX}{digest}"


# ============================================================
# DATE
# ============================================================

def parse_row_date(
    date_str: str
) -> Optional[date]:

    date_str = (
        date_str or ""
    ).strip()

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

    credentials = get_credentials()

    return build(
        "calendar",
        "v3",
        credentials=credentials,
        cache_discovery=False
    )


# ============================================================
# ERROR HELPERS
# ============================================================

def get_error_reason(
    error: HttpError
) -> str:

    """
    Extract Google's error reason.
    """

    try:

        content = error.content

        if isinstance(content, bytes):

            content = content.decode(
                "utf-8",
                errors="ignore"
            )

        match = re.search(
            r'"reason"\s*:\s*"([^"]+)"',
            content
        )

        if match:

            return match.group(1)

    except Exception:

        pass

    return ""


def is_retryable_error(
    error: HttpError
) -> bool:

    """
    Determine whether Google recommends retrying.
    """

    status = getattr(
        error.resp,
        "status",
        None
    )

    reason = get_error_reason(error)

    # Google Calendar rate limits can appear as
    # either 403 or 429.
    if reason == "rateLimitExceeded":

        return True

    if reason == "userRateLimitExceeded":

        return True

    if status in (429, 500, 502, 503, 504):

        return True

    return False


# ============================================================
# RETRY WRAPPER
# ============================================================

def execute_with_retry(
    request_factory,
    operation_name: str,
    stats: Optional[SyncStats] = None
):

    """
    Execute a Google Calendar request with
    exponential backoff.

    request_factory must return a Google API
    request object.
    """

    consecutive_rate_limits = 0

    for attempt in range(
        MAX_RETRIES + 1
    ):

        try:

            throttle()

            request = request_factory()

            result = request.execute()

            # Successful request.
            consecutive_rate_limits = 0

            return result

        except HttpError as error:

            status = getattr(
                error.resp,
                "status",
                None
            )

            reason = get_error_reason(error)

            retryable = is_retryable_error(
                error
            )

            if not retryable:

                raise

            if stats is not None:

                stats.retried += 1

            if (
                reason == "rateLimitExceeded"
                or reason == "userRateLimitExceeded"
                or status == 429
            ):

                consecutive_rate_limits += 1

            else:

                consecutive_rate_limits = 0

            if attempt >= MAX_RETRIES:

                log.error(
                    "%s failed after %d retries.",
                    operation_name,
                    MAX_RETRIES
                )

                raise

            # Exponential backoff:
            #
            # 2
            # 4
            # 8
            # 16
            # 32
            # 60...
            #
            backoff = min(
                BACKOFF_INITIAL
                * (2 ** attempt),
                BACKOFF_MAX
            )

            # Random jitter.
            backoff += random.uniform(
                0.0,
                1.5
            )

            # If we repeatedly hit rate limits,
            # increase the cooldown.
            if consecutive_rate_limits >= 3:

                cooldown(
                    RATE_LIMIT_COOLDOWN
                )

                consecutive_rate_limits = 0

            log.warning(
                "%s: Google returned %s "
                "(reason=%s). "
                "Retrying in %.1f seconds "
                "(attempt %d/%d)...",
                operation_name,
                status,
                reason or "unknown",
                backoff,
                attempt + 1,
                MAX_RETRIES
            )

            time.sleep(backoff)


# ============================================================
# TEST CALENDAR ACCESS
# ============================================================

def test_calendar_access(
    service,
    calendar_id: str
):

    log.info(
        "Testing Google Calendar access..."
    )

    try:

        calendar = execute_with_retry(
            lambda:
                service.calendars().get(
                    calendarId=calendar_id
                ),
            "Calendar access test"
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

    except HttpError as error:

        log.error(
            "Unable to access Google Calendar."
        )

        log.error(
            "Calendar ID used: ...%s",
            calendar_id[-30:]
        )

        log.error(
            "Google Calendar API error: %s",
            error
        )

        raise RuntimeError(
            "The service account cannot access "
            "the specified Google Calendar."
        ) from error


# ============================================================
# EVENT BODY
# ============================================================

def row_to_event_body(
    row: dict
) -> dict:

    d = parse_row_date(
        row.get("date", "")
    )

    if d is None:

        raise ValueError(
            f"Invalid date: {row.get('date')}"
        )

    # Google Calendar all-day events use
    # an exclusive end date.
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

        "summary": (
            row["exam"]
            or "Exam event"
        ),

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

                "sync_source":
                    SYNC_SOURCE_TAG,

                "row_key":
                    event_id

            }

        }

    }

    if row.get("exam_url"):

        body["source"] = {

            "title":
                "Exam details",

            "url":
                row["exam_url"]

        }

    return body


# ============================================================
# INSERT EVENT
# ============================================================

def insert_event(
    service,
    calendar_id: str,
    event_id: str,
    body: dict,
    stats: SyncStats
):

    body = dict(body)

    body["id"] = event_id

    return execute_with_retry(
        lambda:
            service.events().insert(
                calendarId=calendar_id,
                body=body
            ),
        f"Insert event {event_id}",
        stats
    )


# ============================================================
# UPDATE EVENT
# ============================================================

def update_event(
    service,
    calendar_id: str,
    event_id: str,
    body: dict,
    stats: SyncStats
):

    body = dict(body)

    body["id"] = event_id

    return execute_with_retry(
        lambda:
            service.events().update(
                calendarId=calendar_id,
                eventId=event_id,
                body=body
            ),
        f"Update event {event_id}",
        stats
    )


# ============================================================
# UPSERT EVENT
# ============================================================

def upsert_event(
    service,
    calendar_id: str,
    event_id: str,
    body: dict,
    stats: SyncStats
) -> str:

    """
    Insert first.

    If event already exists (409),
    update it.

    Both operations have retry handling.
    """

    try:

        insert_event(
            service,
            calendar_id,
            event_id,
            body,
            stats
        )

        return "created"

    except HttpError as error:

        status = getattr(
            error.resp,
            "status",
            None
        )

        # Existing event.
        if status == 409:

            update_event(
                service,
                calendar_id,
                event_id,
                body,
                stats
            )

            return "updated"

        raise


# ============================================================
# FIND EXISTING SYNC EVENTS
# ============================================================

def list_existing_synced_event_ids(
    service,
    calendar_id: str,
    stats: SyncStats
) -> set[str]:

    ids = set()

    page_token = None

    while True:

        response = execute_with_retry(

            lambda:
                service.events().list(

                    calendarId=calendar_id,

                    privateExtendedProperty=(
                        f"sync_source="
                        f"{SYNC_SOURCE_TAG}"
                    ),

                    showDeleted=False,

                    singleEvents=True,

                    pageToken=page_token,

                    maxResults=2500

                ),

            "List existing synced events",

            stats
        )

        for item in response.get(
            "items",
            []
        ):

            if item.get("id"):

                ids.add(
                    item["id"]
                )

        page_token = response.get(
            "nextPageToken"
        )

        if not page_token:

            break

    return ids


# ============================================================
# DELETE EVENT
# ============================================================

def delete_event(
    service,
    calendar_id: str,
    event_id: str,
    stats: SyncStats
):

    try:

        execute_with_retry(

            lambda:
                service.events().delete(

                    calendarId=calendar_id,

                    eventId=event_id

                ),

            f"Delete event {event_id}",

            stats
        )

    except HttpError as error:

        status = getattr(
            error.resp,
            "status",
            None
        )

        # Already deleted.
        if status == 410:

            return

        raise


# ============================================================
# MAIN SYNC
# ============================================================

def run_sync() -> SyncStats:

    stats = SyncStats()

    # --------------------------------------------------------
    # CALENDAR ID
    # --------------------------------------------------------

    calendar_id = (
        validate_and_clean_calendar_id(
            os.environ.get(
                "GOOGLE_CALENDAR_ID",
                ""
            )
        )
    )

    # --------------------------------------------------------
    # SHEET TAB
    # --------------------------------------------------------

    tab_name = (
        os.environ.get(
            "SHEET_TAB_NAME"
        )
        or None
    )

    log.info(
        "Reading rows%s from Google Sheet...",
        (
            f" from tab '{tab_name}'"
            if tab_name
            else " (all tabs)"
        )
    )

    # --------------------------------------------------------
    # READ SHEET
    # --------------------------------------------------------

    rows = read_all_rows(
        tab_name=tab_name
    )

    log.info(
        "Read %d row(s).",
        len(rows)
    )

    if not rows:

        log.warning(
            "No rows found in Google Sheet."
        )

        return stats

    # --------------------------------------------------------
    # BUILD CALENDAR SERVICE
    # --------------------------------------------------------

    service = build_calendar_service()

    # --------------------------------------------------------
    # TEST CALENDAR
    # --------------------------------------------------------

    test_calendar_access(
        service,
        calendar_id
    )

    desired_ids = set()

    # --------------------------------------------------------
    # REMOVE DUPLICATE ROWS
    # --------------------------------------------------------

    processed_ids = set()

    unique_rows = []

    for row in rows:

        exam = str(
            row.get("exam", "")
        ).strip()

        date_str = str(
            row.get("date", "")
        ).strip()

        event_type = str(
            row.get(
                "event_type",
                ""
            )
        ).strip()

        if not exam or not date_str:

            unique_rows.append(row)

            continue

        event_id = make_event_id(
            exam,
            date_str,
            event_type
        )

        if event_id in processed_ids:

            log.info(
                "Skipping duplicate row: "
                "%s (%s)",
                exam,
                date_str
            )

            stats.skipped += 1

            continue

        processed_ids.add(
            event_id
        )

        unique_rows.append(row)

    log.info(
        "Rows after duplicate filtering: %d",
        len(unique_rows)
    )

    # --------------------------------------------------------
    # SYNC EVENTS
    # --------------------------------------------------------

    for index, row in enumerate(
        unique_rows,
        start=1
    ):

        d = parse_row_date(
            row.get("date", "")
        )

        if d is None:

            log.warning(
                "[%d/%d] Skipping invalid date: %r",
                index,
                len(unique_rows),
                row.get("date")
            )

            stats.skipped += 1

            continue

        exam = str(
            row.get(
                "exam",
                ""
            )
        ).strip()

        if not exam:

            log.warning(
                "[%d/%d] Skipping row "
                "with empty exam name.",
                index,
                len(unique_rows)
            )

            stats.skipped += 1

            continue

        date_str = str(
            row.get(
                "date",
                ""
            )
        ).strip()

        event_type = str(
            row.get(
                "event_type",
                ""
            )
        ).strip()

        event_id = make_event_id(
            exam,
            date_str,
            event_type
        )

        desired_ids.add(
            event_id
        )

        try:

            body = row_to_event_body(
                row
            )

            action = upsert_event(
                service,
                calendar_id,
                event_id,
                body,
                stats
            )

            if action == "created":

                stats.created += 1

                log.info(
                    "[%d/%d] Created: "
                    "%s (%s)",
                    index,
                    len(unique_rows),
                    exam,
                    date_str
                )

            else:

                stats.updated += 1

                log.info(
                    "[%d/%d] Updated: "
                    "%s (%s)",
                    index,
                    len(unique_rows),
                    exam,
                    date_str
                )

        except HttpError as error:

            stats.failed += 1

            reason = get_error_reason(
                error
            )

            log.error(
                "[%d/%d] Failed to sync "
                "'%s' (%s): "
                "HTTP %s reason=%s",
                index,
                len(unique_rows),
                exam,
                date_str,
                getattr(
                    error.resp,
                    "status",
                    "?"
                ),
                reason or "unknown"
            )

            # If Google is still rate limiting,
            # give the API additional recovery time.
            if (
                reason == "rateLimitExceeded"
            ):

                cooldown(
                    RATE_LIMIT_COOLDOWN
                )

        except Exception as error:

            stats.failed += 1

            log.error(
                "[%d/%d] Unexpected failure "
                "for '%s' (%s): %s",
                index,
                len(unique_rows),
                exam,
                date_str,
                error,
                exc_info=True
            )

    # --------------------------------------------------------
    # DELETE REMOVED EVENTS
    # --------------------------------------------------------

    if DELETE_REMOVED_EVENTS:

        log.info(
            "DELETE_REMOVED_EVENTS enabled."
        )

        existing_ids = (
            list_existing_synced_event_ids(
                service,
                calendar_id,
                stats
            )
        )

        stale_ids = (
            existing_ids - desired_ids
        )

        log.info(
            "Found %d stale synced event(s).",
            len(stale_ids)
        )

        for event_id in stale_ids:

            try:

                delete_event(
                    service,
                    calendar_id,
                    event_id,
                    stats
                )

                stats.deleted += 1

                log.info(
                    "Deleted stale event: %s",
                    event_id
                )

            except HttpError as error:

                stats.failed += 1

                log.error(
                    "Failed to delete stale "
                    "event %s: %s",
                    event_id,
                    error
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

    log.info(
        "=========================================="
    )

    log.info(
        "Google Calendar Exam Sync"
    )

    log.info(
        "Request delay: %.2f seconds",
        MIN_REQUEST_DELAY
    )

    log.info(
        "Maximum retries: %d",
        MAX_RETRIES
    )

    log.info(
        "=========================================="
    )

    try:

        stats = run_sync()

    except RuntimeError as error:

        log.error(
            "Configuration error: %s",
            error
        )

        sys.exit(1)

    except Exception as error:

        log.error(
            "Unexpected error during sync: %s",
            error,
            exc_info=True
        )

        sys.exit(1)

    log.info(
        "=========================================="
    )

    log.info(
        "SYNC COMPLETE"
    )

    log.info(
        "%s",
        stats.summary()
    )

    log.info(
        "=========================================="
    )

    if stats.failed > 0:

        log.error(
            "%d row(s)/event(s) failed.",
            stats.failed
        )

        sys.exit(2)

    log.info(
        "All events synchronized successfully."
    )

    sys.exit(0)


if __name__ == "__main__":

    main()
```
