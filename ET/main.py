"""Single safe entry point: scrape, allowlist, compare, sheet, calendar, Chat."""
from __future__ import annotations

import logging
import os
import sys

import calendar_sync
import google_chat
import google_sheets
import scraper
from change_detector import detect_changes, filter_allowed_exams, load_allowed_names, normalize_exam_name

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("main")
TARGET_MONTH = os.getenv("TARGET_MONTH", "August 2026")


def _row_from_scrape(item: dict, allowed: set[str], canonical_names: dict[str, str]) -> dict | None:
    """Convert one Shiksha table row into the normalized sheet schema.

    Shiksha's table has separate Label (exam) and Event columns.  The old
    implementation incorrectly searched the Event text for the exam name,
    which made legitimate rows disappear when the event title did not repeat
    the label.
    """
    label = str(item.get("label") or "").strip()
    event_text = str(item.get("Event") or "").strip()
    date = str(item.get("date") or "").strip()
    if not date or not label or not event_text:
        return None

    normalized_label = normalize_exam_name(label)
    if normalized_label not in allowed:
        return None

    # Keep the spelling/casing used in the Exam_Name worksheet.
    exam = canonical_names.get(normalized_label, label)
    return {
        "date": date,
        "exam": exam,
        "event": event_text,
        "event_type": event_text,
        "exam_url": "",
    }


def main() -> int:
    try:
        log.info("Shiksha Exam Calendar Sync | Target Month: %s", TARGET_MONTH)
        raw_allowed = google_sheets.read_exam_names()
        canonical_names = {normalize_exam_name(value): str(value).strip() for value in raw_allowed if str(value).strip()}
        allowed = set(canonical_names)
        log.info("Allowed exams: %d", len(allowed))
        if not allowed:
            raise RuntimeError("Exam_Name is empty; refusing to overwrite the month worksheet.")

        scraped = scraper.scrape_shiksha()
        if not scraped:
            # A blocked/empty scrape (e.g. Shiksha returning HTTP 403 to
            # the runner) is an EXPECTED, recurring condition until the
            # source-side block clears — not a code defect. The Sheet
            # and Calendar are deliberately left untouched (same safety
            # rule as before), a Chat alert still goes out so this
            # doesn't fail silently, but the workflow run itself is no
            # longer marked as a failure. Genuine bugs below (bad
            # credentials, Sheets/Calendar API errors, etc.) still hit
            # the except block and still return 1.
            log.warning(
                "Scraper returned no events; leaving the '%s' worksheet "
                "and Calendar untouched. This usually means Shiksha (or "
                "a WAF in front of it) blocked the request — check the "
                "scraper-debug-snapshot artifact from this run.",
                TARGET_MONTH,
            )
            google_chat.send_failure_message(
                "Scraper",
                "No events returned (likely blocked by the source). "
                "Existing Sheet/Calendar data was NOT modified. "
                "See the scraper-debug-snapshot artifact for details.",
            )
            return 0

        candidate_rows = [row for item in scraped if (row := _row_from_scrape(item, allowed, canonical_names))]
        rows = google_sheets.deduplicate_sheet_rows(
            filter_allowed_exams(candidate_rows, allowed)
        )
        log.info("Scraped events: %d | After Exam_Name filtering: %d", len(scraped), len(rows))

        # Never erase a previously good month because the source structure
        # changed or the Exam_Name allowlist stopped matching. A successful
        # scrape with zero allowed rows is a data-integrity failure, not an
        # empty month.
        if scraped and not rows:
            raise RuntimeError(
                f"Scraper returned {len(scraped)} event(s), but 0 matched the Exam_Name allowlist. "
                "Refusing to overwrite the month worksheet. Check the Shiksha Label column "
                "and the Exam_Name worksheet."
            )

        previous = google_sheets.read_all_rows(TARGET_MONTH)
        changes = detect_changes(previous, rows)
        log.info("New=%d Updated=%d Unchanged=%d Removed=%d", *(len(changes[k]) for k in ("new", "updated", "unchanged", "removed")))
        google_sheets.write_month_data(rows, TARGET_MONTH)
        stats = calendar_sync.run_sync(rows=rows, tab_name=TARGET_MONTH)
        if stats.failed:
            raise RuntimeError(f"Calendar synchronization had {stats.failed} failed event(s).")
        for row in changes["new"]:
            google_chat.send_new_exam_message(row)
        for update in changes["updated"]:
            google_chat.send_update_message(update["previous"], update["current"])
        log.info("Sync completed successfully.")
        return 0
    except Exception as exc:
        log.exception("Synchronization failed: %s", exc)
        google_chat.send_failure_message("Synchronization", str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
