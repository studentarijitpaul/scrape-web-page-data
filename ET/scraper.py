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
from typing import Optional, Callable, Any

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from google_sheets import get_credentials, read_all_rows


# ============================================================
# CONFIGURATION
# ============================================================

DEFAULT_TIMEZONE = "Asia/Kolkata"

SYNC_SOURCE_TAG = "shiksha_exam_sync"

# Google Calendar event IDs may contain only a-v and 0-9.
EVENT_ID_PREFIX = "evt"

# ------------------------------------------------------------
# RATE LIMIT PROTECTION
# ------------------------------------------------------------

# Minimum delay between Calendar API requests.
# Increase this if Google still returns rateLimitExceeded.
MIN_API_DELAY = 1.5

# Maximum number of retries for a single API request.
MAX_RETRIES = 7

# Initial exponential-backoff delay.
INITIAL_BACKOFF = 2.0

# Maximum exponential-backoff delay.
MAX_BACKOFF = 60.0

# Add random jitter so requests don't happen at exact intervals.
JITTER_MIN = 0.5
JITTER_MAX = 1.5

# Pause after a successful event operation.
# This intentionally slows the sync down to avoid quota bursts.
EVENT_DELAY = 1.0

# Set this to True if you want removed events deleted.
DELETE_REMOVED_EVENTS = False


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

log = logging.getLogger("calendar_sync")


# ============================================================
# GLOBAL API RATE LIMITER
# ============================================================

_last_api_request = 0.0


def wait_before_api_request() -> None:
    """
    Ensure there is a minimum delay between Google Calendar API
    requests.

    This prevents the script from sending a burst of requests.
    """

    global _last_api_request

    now = time.monotonic()

    elapsed = now - _last_api_request

    if elapsed < MIN_API_DELAY:
        sleep_time = MIN_API_DELAY - elapsed
        time.sleep(sleep_time)

    _last_api_request = time.monotonic()


# ============================================================
# RETRY HELPERS
# ============================================================

RETRYABLE_STATUS_CODES = {
    403,
    429,
    500,
    502,
    503,
    504,
}


RETRYABLE_REASONS = {
    "rateLimitExceeded",
    "userRateLimitExceeded",
    "backendError",
    "internalError",
}


def get_http_error_reason(error: HttpError) -> str:
    """
    Extract Google's error reason from HttpError.
    """

    try:
        content = error.content

        if isinstance(content, bytes):
            content = content.decode("utf-8", errors="ignore")

        match = re.search(
            r'"reason"\s*:\s*"([^"]+)"',
            content,
        )

        if match:
            return match.group(1)

    except Exception:
        pass

    return ""


def is_retryable_http_error(error: HttpError) -> bool:
    """
    Determine whether a Google API error should be retried.
    """

    status = getattr(error.resp, "status", None)

    if status in RETRYABLE_STATUS_CODES:
        return True

    reason = get_http_error_reason(error)

    if reason in RETRYABLE_REASONS:
        return True

    return False


def execute_with_retry(
    operation: Callable[[], Any],
    operation_name: str,
) -> Any:
    """
    Execute a Google Calendar API operation with:

    - Minimum request spacing
    - Exponential backoff
    - Random jitter
    - Retry handling for 403/429/5xx errors
    """

    for attempt in range(1, MAX_RETRIES + 1):

        wait_before_api_request()

        try:

            result = operation()

            return result

        except HttpError as error:

            status = getattr(error.resp, "status", None)

            reason = get_http_error_reason(error)

            if not is_retryable_http_error(error):

                log.error(
                    "%s failed permanently: HTTP %s reason=%s",
                    operation_name,
                    status,
                    reason,
                )

                raise

            if attempt >= MAX_RETRIES:

                log.error(
                    "%s failed after %d attempts: "
                    "HTTP %s reason=%s",
                    operation_name,
                    attempt,
                    status,
                    reason,
                )

                raise

            # ------------------------------------------------
            # EXPONENTIAL BACKOFF
            # ------------------------------------------------

            backoff = min(
                INITIAL_BACKOFF * (2 ** (attempt - 1)),
                MAX_BACKOFF,
            )

            jitter = random.uniform(
                JITTER_MIN,
                JITTER_MAX,
            )

            sleep_time = backoff * jitter

            log.warning(
                "%s: Google API rate/backend limit "
                "(HTTP %s, reason=%s). "
                "Retry %d/%d in %.1f seconds...",
                operation_name,
                status,
                reason or "unknown",
                attempt,
                MAX_RETRIES,
                sleep_time,
            )

            time.sleep(sleep_time)

    raise RuntimeError(
        f"Retry loop exited unexpectedly: {operation_name}"
    )


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
# EVENT ID
# ============================================================

def make_event_id(
    exam: str,
    date_str: str,
    event_type: str = "",
) -> str:
    """
    Generate a deterministic Google Calendar event ID.

    The generated ID contains only:
        a-v
        0-9

    This ensures compatibility with Google Calendar.
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

    # Google Calendar event IDs must contain only a-v and 0-9.
    if not re.fullmatch(
        r"[a-v0-9]{5,1024}",
        event_id,
    ):
        raise ValueError(
            f"Generated invalid Google Calendar "
            f"event ID: {event_id}"
        )

    return event_id


# ============================================================
# DATE PARSING
# ============================================================

def parse_row_date(
    date_str: str,
) -> Optional[date]:
    """
    Convert YYYY-MM-DD into a date object.
    """

    date_str = (date_str or "").strip()

    if not date_str:
        return None

    formats = [
        "%Y-%m-%d",
        "%d-%m-%Y",
        "%d/%m/%Y",
        "%Y/%m/%d",
    ]

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

    creds = get_credentials()

    return build(
        "calendar",
        "v3",
        credentials=creds,
        cache_discovery=False,
    )


# ============================================================
# TEST CALENDAR ACCESS
# ============================================================

def test_calendar_access(
    service,
    calendar_id: str,
):
    """
    Verify Calendar access before syncing.
    """

    log.info("Testing Google Calendar access...")

    result = execute_with_retry(
        lambda: service.calendars().get(
            calendarId=calendar_id
        ).execute(),
        "Calendar access test",
    )

    log.info(
        "Google Calendar connection successful!"
    )

    log.info(
        "Calendar name: %s",
        result.get("summary"),
    )

    log.info(
        "Calendar timezone: %s",
        result.get("timeZone"),
    )

    return result


# ============================================================
# EVENT BODY
# ============================================================

def row_to_event_body(row: dict) -> dict:
    """
    Convert a Google Sheet row into a Google Calendar
    all-day event.
    """

    d = parse_row_date(
        row.get("date", "")
    )

    if d is None:

        raise ValueError(
            f"Invalid date: {row.get('date')}"
        )

    end = d + timedelta(days=1)

    exam = str(
        row.get("exam", "")
    ).strip()

    label = str(
        row.get("label", "")
    ).strip()

    event = str(
        row.get("Event", "")
    ).strip()

    description_lines = []

    if label:
        description_lines.append(
            f"Label: {label}"
        )

    if event:
        description_lines.append(
            f"Event: {event}"
        )

    description_lines.append(
        f"Source: {SYNC_SOURCE_TAG}"
    )

    body = {
        "summary": exam or "Exam Event",

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
                "exam": exam,
                "date": d.isoformat(),
            }
        },
    }

    return body


# ============================================================
# UPSERT EVENT
# ============================================================

def upsert_event(
    service,
    calendar_id: str,
    event_id: str,
    body: dict,
) -> str:
    """
    Create or update an event.

    Uses deterministic event IDs so that repeated runs
    update existing events instead of creating duplicates.
    """

    # --------------------------------------------------------
    # TRY UPDATE FIRST
    # --------------------------------------------------------

    def update_operation():

        return service.events().update(
            calendarId=calendar_id,
            eventId=event_id,
            body=body,
            sendUpdates="none",
        ).execute()

    try:

        execute_with_retry(
            update_operation,
            f"Update event {event_id}",
        )

        return "updated"

    except HttpError as error:

        status = getattr(
            error.resp,
            "status",
            None,
        )

        # Event does not exist.
        if status != 404:

            raise

    # --------------------------------------------------------
    # EVENT DOES NOT EXIST
    # CREATE IT
    # --------------------------------------------------------

    def insert_operation():

        return service.events().insert(
            calendarId=calendar_id,
            body=body,
            sendUpdates="none",
        ).execute()

    try:

        execute_with_retry(
            insert_operation,
            f"Create event {event_id}",
        )

        return "created"

    except HttpError as error:

        status = getattr(
            error.resp,
            "status",
            None,
        )

        # Another process may have created it between
        # our update and insert.
        if status == 409:

            execute_with_retry(
                update_operation,
                f"Update after conflict {event_id}",
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
    """

    def delete_operation():

        return service.events().delete(
            calendarId=calendar_id,
            eventId=event_id,
        ).execute()

    try:

        execute_with_retry(
            delete_operation,
            f"Delete event {event_id}",
        )

    except HttpError as error:

        status = getattr(
            error.resp,
            "status",
            None,
        )

        # Already deleted.
        if status == 410:
            return

        # Not found.
        if status == 404:
            return

        raise


# ============================================================
# LIST EXISTING SYNC EVENTS
# ============================================================

def list_existing_synced_event_ids(
    service,
    calendar_id: str,
) -> set[str]:
    """
    Find events previously created by this sync script.

    Pagination is handled automatically.
    """

    ids = set()

    page_token = None

    while True:

        def list_operation():

            request = service.events().list(
                calendarId=calendar_id,
                maxResults=250,
                singleEvents=False,
                showDeleted=False,
                pageToken=page_token,
            )

            return request.execute()

        response = execute_with_retry(
            list_operation,
            "List Calendar events",
        )

        for item in response.get(
            "items",
            [],
        ):

            private_props = (
                item
                .get("extendedProperties", {})
                .get("private", {})
            )

            if (
                private_props.get("sync_source")
                == SYNC_SOURCE_TAG
            ):

                event_id = item.get("id")

                if event_id:
                    ids.add(event_id)

        page_token = response.get(
            "nextPageToken"
        )

        if not page_token:
            break

    return ids


# ============================================================
# ROW NORMALIZATION
# ============================================================

def normalize_row(row: dict) -> Optional[dict]:
    """
    Clean a spreadsheet row.
    """

    if not row:
        return None

    cleaned = {}

    for key, value in row.items():

        if key is None:
            continue

        clean_key = str(key).strip()

        clean_value = (
            str(value).strip()
            if value is not None
            else ""
        )

        cleaned[clean_key] = clean_value

    exam = cleaned.get("exam", "")
    date_str = cleaned.get("date", "")

    if not exam or not date_str:
        return None

    return cleaned


# ============================================================
# DEDUPLICATION
# ============================================================

def deduplicate_rows(
    rows: list[dict],
) -> list[dict]:
    """
    Remove duplicate exam/date/event combinations.

    This is important because duplicate spreadsheet rows
    otherwise cause unnecessary Calendar API requests.
    """

    unique = {}

    duplicates = 0

    for row in rows:

        normalized = normalize_row(row)

        if normalized is None:
            continue

        key = (
            normalized.get("exam", "")
            .strip()
            .lower(),

            normalized.get("date", "")
            .strip(),

            normalized.get("Event", "")
            .strip()
            .lower(),
        )

        if key in unique:

            duplicates += 1

            continue

        unique[key] = normalized

    log.info(
        "Removed %d duplicate row(s).",
        duplicates,
    )

    return list(unique.values())


# ============================================================
# MAIN SYNC
# ============================================================

def run_sync():

    stats = SyncStats()

    # --------------------------------------------------------
    # CALENDAR ID
    # --------------------------------------------------------

    calendar_id = os.getenv(
        "GOOGLE_CALENDAR_ID"
    )

    if not calendar_id:

        raise RuntimeError(
            "GOOGLE_CALENDAR_ID environment variable "
            "is not configured."
        )

    log.info(
        "Calendar ID detected: %s",
        calendar_id,
    )

    # --------------------------------------------------------
    # READ GOOGLE SHEET
    # --------------------------------------------------------

    log.info(
        "Reading rows (all tabs) from Google Sheet..."
    )

    rows = read_all_rows()

    log.info(
        "Read %d row(s).",
        len(rows),
    )

    # --------------------------------------------------------
    # DEDUPLICATE
    # --------------------------------------------------------

    rows = deduplicate_rows(rows)

    log.info(
        "Processing %d unique row(s).",
        len(rows),
    )

    if not rows:

        log.warning(
            "No valid rows found."
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
        calendar_id,
    )

    # --------------------------------------------------------
    # TRACK DESIRED EVENTS
    # --------------------------------------------------------

    desired_ids = set()

    # --------------------------------------------------------
    # PROCESS ROWS
    # --------------------------------------------------------

    for index, row in enumerate(
        rows,
        start=1,
    ):

        exam = row.get(
            "exam",
            "",
        ).strip()

        date_str = row.get(
            "date",
            "",
        ).strip()

        event_type = row.get(
            "Event",
            "",
        ).strip()

        log.info(
            "Processing %d/%d: %s (%s)",
            index,
            len(rows),
            exam,
            date_str,
        )

        # ----------------------------------------------------
        # VALIDATE DATE
        # ----------------------------------------------------

        parsed_date = parse_row_date(
            date_str
        )

        if parsed_date is None:

            stats.skipped += 1

            log.warning(
                "Skipping invalid date: %s (%s)",
                exam,
                date_str,
            )

            continue

        # ----------------------------------------------------
        # EVENT ID
        # ----------------------------------------------------

        event_id = make_event_id(
            exam,
            date_str,
            event_type,
        )

        desired_ids.add(event_id)

        # ----------------------------------------------------
        # EVENT BODY
        # ----------------------------------------------------

        try:

            body = row_to_event_body(row)

        except ValueError as error:

            stats.skipped += 1

            log.warning(
                "Skipping row: %s",
                error,
            )

            continue

        # ----------------------------------------------------
        # CREATE / UPDATE
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
                    date_str,
                )

            elif action == "updated":

                stats.updated += 1

                log.info(
                    "Updated: %s (%s)",
                    exam,
                    date_str,
                )

            # ------------------------------------------------
            # EXTRA SPACING BETWEEN EVENTS
            # ------------------------------------------------

            time.sleep(
                EVENT_DELAY
                * random.uniform(
                    0.8,
                    1.2,
                )
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
                "Unexpected failure for '%s' (%s): %s",
                exam,
                date_str,
                error,
                exc_info=True,
            )

    # --------------------------------------------------------
    # DELETE REMOVED EVENTS
    # --------------------------------------------------------

    if DELETE_REMOVED_EVENTS:

        log.info(
            "DELETE_REMOVED_EVENTS enabled."
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

                    time.sleep(
                        EVENT_DELAY
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
                "Failed while scanning existing "
                "Calendar events: %s",
                error,
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

        log.warning(
            "%d row(s)/event(s) failed.",
            stats.failed,
        )

        # IMPORTANT:
        # We do NOT immediately fail GitHub Actions.
        #
        # Some rows may have failed because Google temporarily
        # throttled the API. The successful rows are still valid.
        #
        # If you want GitHub Actions to fail when ANY row fails,
        # change this back to:
        #
        # sys.exit(2)

        sys.exit(0)

    sys.exit(0)


if __name__ == "__main__":
    main()
