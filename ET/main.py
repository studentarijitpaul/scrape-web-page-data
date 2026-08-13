"""Single safe entry point: scrape, allowlist, compare, sheet, calendar, Chat."""
from __future__ import annotations

import logging
import os
import re
import sys

import calendar_sync
import google_chat
import google_sheets
import scraper
from change_detector import detect_changes, filter_allowed_exams, load_allowed_names

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("main")
TARGET_MONTH = os.getenv("TARGET_MONTH", "August 2026")


def _row_from_scrape(item: dict, allowed: set[str]) -> dict | None:
    # Shiksha's rendered title is the only name field supplied by the existing scraper.
    title = (item.get("label") or item.get("Event") or "").strip()
    normalized_title = re.sub(r"[^\w]+", " ", title.casefold()).strip()
    matches = [name for name in allowed if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", normalized_title)]
    if len(matches) != 1:
        return None
    exam = matches[0]
    return {"date": item.get("date", "").strip(), "exam": exam.upper(), "event": title, "event_type": title, "exam_url": ""}


def main() -> int:
    try:
        log.info("Shiksha Exam Calendar Sync | Target Month: %s", TARGET_MONTH)
        allowed = load_allowed_names(google_sheets.read_exam_names())
        log.info("Allowed exams: %d", len(allowed))
        if not allowed:
            raise RuntimeError("Exam_Name is empty; refusing to overwrite the month worksheet.")
        scraped = scraper.scrape_shiksha()
        if not scraped:
            raise RuntimeError("Scraper returned no events; refusing to treat this as removals.")
        candidate_rows = [row for item in scraped if (row := _row_from_scrape(item, allowed))]
        rows = filter_allowed_exams(candidate_rows, allowed)
        log.info("Scraped exams: %d | After Exam_Name filtering: %d", len(scraped), len(rows))
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
