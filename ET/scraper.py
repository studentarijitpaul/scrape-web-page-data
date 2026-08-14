"""
Shiksha Exam Calendar Scraper
=============================

Purpose:
    Scrape exam calendar data from Shiksha and write it to Google Sheets.

Google Calendar is intentionally NOT handled by this script.
Google Calendar synchronization should be handled separately by:

    calendar_sync.py

Required environment variables:
    GOOGLE_SERVICE_ACCOUNT_JSON
    GOOGLE_SHEET_ID

Optional environment variables:
    TARGET_MONTH
    SHIKSHA_URL

Example:
    TARGET_MONTH="August 2026" python scraper.py

GitHub Actions:
    GOOGLE_SERVICE_ACCOUNT_JSON=${{ secrets.GOOGLE_SERVICE_ACCOUNT_JSON }}
    GOOGLE_SHEET_ID=${{ secrets.GOOGLE_SHEET_ID }}
"""

import os
import sys
import json
import time
import random
import logging
from datetime import datetime
from typing import List, Dict, Optional

import gspread
from google.oauth2.service_account import Credentials

from playwright.sync_api import (
    sync_playwright,
    TimeoutError as PlaywrightTimeoutError,
)

import google_chat


# ============================================================
# CONFIGURATION
# ============================================================

SHIKSHA_URL = os.getenv(
    "SHIKSHA_URL",
    "https://www.shiksha.com/engineering/resources/exam-calendar"
)

TARGET_MONTH = os.getenv(
    "TARGET_MONTH",
    "August 2026"
)

GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "").strip()

GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv(
    "GOOGLE_SERVICE_ACCOUNT_JSON",
    ""
).strip()

# Google Sheet worksheet/tab where data will be written.
OUTPUT_WORKSHEET = os.getenv(
    "OUTPUT_WORKSHEET",
    "Exam Calendar"
)

# Whether to clear the existing worksheet before writing.
CLEAR_SHEET = os.getenv(
    "CLEAR_SHEET",
    "true"
).lower() == "true"

# Browser settings
HEADLESS = True

PAGE_TIMEOUT = 60_000

# Random delay settings
MIN_DELAY = 1.0
MAX_DELAY = 3.0

# Retry settings: a WAF/anti-bot challenge or a slow page load is
# sometimes transient, so a fresh browser session on the next attempt
# can succeed even when the previous one didn't. NOTE: if Shiksha (or a
# CDN/WAF in front of it) is blocking by IP reputation rather than by
# session/fingerprint, retries from the SAME GitHub Actions runner will
# keep hitting the same source IP and will keep failing — see the 403
# diagnostics captured below.
MAX_SCRAPE_ATTEMPTS = int(os.getenv("MAX_SCRAPE_ATTEMPTS", "3"))
RETRY_BACKOFF_SECONDS = 15

# Where to save a screenshot/HTML/response-header dump when a scrape
# attempt returns zero events, so a failure (blocked page vs. genuinely
# empty calendar vs. a structure change) can be diagnosed from the
# GitHub Actions run itself instead of guessing. Uploaded as a CI
# artifact by the workflow.
DEBUG_ARTIFACT_DIR = os.getenv("DEBUG_ARTIFACT_DIR", "debug_artifacts")


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

logger = logging.getLogger(__name__)


# ============================================================
# UTILITY FUNCTIONS
# ============================================================

def random_delay(
    minimum: float = MIN_DELAY,
    maximum: float = MAX_DELAY
) -> None:
    """
    Sleep for a random amount of time.
    """

    delay = random.uniform(minimum, maximum)

    logger.debug(
        "Waiting %.2f seconds...",
        delay
    )

    time.sleep(delay)


def validate_configuration() -> None:
    """
    Validate required environment variables.

    IMPORTANT:
        This function intentionally does NOT check
        GOOGLE_CALENDAR_ID because scraper.py does not
        interact with Google Calendar.
    """

    missing = []

    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        missing.append("GOOGLE_SERVICE_ACCOUNT_JSON")

    if not GOOGLE_SHEET_ID:
        missing.append("GOOGLE_SHEET_ID")

    if missing:
        raise RuntimeError(
            "Missing required environment variable(s): "
            + ", ".join(missing)
        )


def parse_target_month(target_month: str):
    """
    Convert 'August 2026' into a datetime object.
    """

    try:
        return datetime.strptime(
            target_month.strip(),
            "%B %Y"
        )

    except ValueError:
        raise ValueError(
            f"Invalid TARGET_MONTH: '{target_month}'. "
            "Expected format: 'August 2026'."
        )


# ============================================================
# GOOGLE SHEETS
# ============================================================

def get_google_credentials():
    """
    Create Google service-account credentials.
    """

    try:
        credentials_info = json.loads(
            GOOGLE_SERVICE_ACCOUNT_JSON
        )

    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON."
        ) from exc

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    credentials = Credentials.from_service_account_info(
        credentials_info,
        scopes=scopes,
    )

    return credentials


def connect_to_google_sheet():
    """
    Connect to the Google Sheet.
    """

    logger.info("Connecting to Google Sheets...")

    credentials = get_google_credentials()

    client = gspread.authorize(credentials)

    spreadsheet = client.open_by_key(
        GOOGLE_SHEET_ID
    )

    logger.info(
        "Connected to Google Sheet: %s",
        spreadsheet.title
    )

    return spreadsheet


def get_or_create_worksheet(spreadsheet):
    """
    Get the configured worksheet.

    If it doesn't exist, create it.
    """

    try:
        worksheet = spreadsheet.worksheet(
            OUTPUT_WORKSHEET
        )

        logger.info(
            "Using existing worksheet: %s",
            OUTPUT_WORKSHEET
        )

        return worksheet

    except gspread.WorksheetNotFound:

        logger.info(
            "Worksheet '%s' not found. Creating it...",
            OUTPUT_WORKSHEET
        )

        worksheet = spreadsheet.add_worksheet(
            title=OUTPUT_WORKSHEET,
            rows=1000,
            cols=10,
        )

        return worksheet


def write_to_google_sheet(
    rows: List[Dict[str, str]]
) -> None:
    """
    Write scraped rows to Google Sheets.
    """

    spreadsheet = connect_to_google_sheet()

    worksheet = get_or_create_worksheet(
        spreadsheet
    )

    if CLEAR_SHEET:
        logger.info(
            "Clearing worksheet..."
        )

        worksheet.clear()

    # --------------------------------------------------------
    # HEADER
    # --------------------------------------------------------

    headers = [
        "date",
        "label",
        "Event",
    ]

    values = [headers]

    # --------------------------------------------------------
    # DATA
    # --------------------------------------------------------

    for row in rows:

        values.append([
            row.get("date", ""),
            row.get("label", ""),
            row.get("Event", ""),
        ])

    logger.info(
        "Writing %d row(s) to Google Sheet...",
        len(rows)
    )

    # Use a single update request rather than updating
    # each cell individually.
    worksheet.update(
        values,
        "A1:C{}".format(len(values)),
    )

    logger.info(
        "Google Sheet updated successfully."
    )

    logger.info(
        "Total rows written: %d",
        len(rows)
    )


# ============================================================
# PLAYWRIGHT
# ============================================================

def launch_browser(playwright):
    """
    Launch Chromium.
    """

    logger.info(
        "Launching headless Chromium..."
    )

    browser = playwright.chromium.launch(
        headless=HEADLESS,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-blink-features=AutomationControlled",
        ],
    )

    return browser


def create_browser_context(browser):
    """
    Create a browser context.
    """

    context = browser.new_context(
        viewport={
            "width": 1440,
            "height": 900,
        },
        locale="en-IN",
        timezone_id="Asia/Kolkata",
        user_agent=(
            "Mozilla/5.0 "
            "(X11; Linux x86_64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/151.0.0.0 "
            "Safari/537.36"
        ),
    )

    return context


# ============================================================
# PAGE LOADING
# ============================================================

def load_shiksha_page(page) -> bool:
    """
    Load the Shiksha exam calendar page.

    Returns True if the response looked like a genuine page load, False
    if the response status indicates the request was blocked (403/429)
    or errored server-side (5xx). This distinction matters: a 403 on the
    very first navigation means the *document itself* was refused by
    Shiksha or a WAF/CDN in front of it, before any client-side
    JavaScript ran. No amount of waiting for selectors or scrolling to
    trigger lazy-loading can produce calendar data in that case, because
    there was never a real calendar page in the response — only a
    block/challenge page.
    """

    logger.info(
        "Fetching [%s] ...",
        SHIKSHA_URL
    )

    blocked = False

    try:

        response = page.goto(
            SHIKSHA_URL,
            wait_until="domcontentloaded",
            timeout=PAGE_TIMEOUT,
        )

        if response:
            logger.info(
                "HTTP status: %s",
                response.status
            )

            if response.status in (403, 429) or response.status >= 500:
                blocked = True

                logger.warning(
                    "[WEBSITE] Non-success status %s on the initial "
                    "document request. This is a source/WAF-side "
                    "response, not a client-rendering issue.",
                    response.status,
                )

                # A handful of response headers are enough to tell a
                # generic Shiksha-side 403 apart from a named WAF/CDN
                # challenge (Cloudflare, Akamai, Imperva, etc.) without
                # doing anything beyond reading headers Playwright
                # already received.
                headers = response.headers
                for header_name in ("server", "cf-ray", "cf-mitigated", "x-akamai-transaction-id", "x-iinfo"):
                    if header_name in headers:
                        logger.warning(
                            "[WEBSITE] Response header %s: %s",
                            header_name,
                            headers[header_name],
                        )

        random_delay()

        return blocked

    except PlaywrightTimeoutError:

        logger.warning(
            "Page load timed out. "
            "Continuing because the page may still be usable."
        )

        return False

    except Exception as exc:

        logger.error(
            "Failed to load Shiksha page: %s",
            exc
        )

        raise


def wait_for_page_content(page) -> None:
    """
    Wait for useful page content.
    """

    logger.info(
        "Waiting for calendar/page content..."
    )

    try:

        page.wait_for_load_state(
            "networkidle",
            timeout=30_000,
        )

    except PlaywrightTimeoutError:

        logger.warning(
            "networkidle timeout. "
            "Continuing with available page content."
        )

    random_delay()


# ============================================================
# CALENDAR DISCOVERY
# ============================================================

def find_calendar_container(page) -> Optional[object]:
    """
    Try to find the calendar container.

    Shiksha may change its HTML structure, so several
    selectors are attempted.
    """

    selectors = [
        ".fc",
        ".fc-view-harness",
        ".fc-view",
        "[class*='calendar']",
        "[id*='calendar']",
    ]

    for selector in selectors:

        try:

            locator = page.locator(
                selector
            ).first

            if locator.count() > 0:

                logger.info(
                    "Calendar container found using selector: %s",
                    selector
                )

                return locator

        except Exception:
            continue

    logger.warning(
        "No obvious calendar container found."
    )

    return None


# ============================================================
# FULLCALENDAR EXTRACTION
# ============================================================

def extract_fullcalendar_events(page) -> List[Dict[str, str]]:
    """
    Attempt to extract events directly from FullCalendar.

    This uses the browser page's JavaScript context.
    """

    logger.info(
        "Attempting FullCalendar event extraction..."
    )

    script = """
    () => {
        const results = [];

        // ----------------------------------------------------
        // Look for common FullCalendar instances.
        // ----------------------------------------------------

        const elements = document.querySelectorAll(
            '.fc, .fc-view-harness, [class*="calendar"]'
        );

        // ----------------------------------------------------
        // Read rendered event elements.
        // ----------------------------------------------------

        const eventElements = document.querySelectorAll(
            '.fc-event, .fc-daygrid-event, .fc-timegrid-event'
        );

        for (const element of eventElements) {

            const text = (element.innerText || '').trim();

            if (!text) {
                continue;
            }

            let date = '';

            const parent = element.closest(
                '[data-date]'
            );

            if (parent) {
                date = parent.getAttribute(
                    'data-date'
                ) || '';
            }

            if (!date) {
                const dayCell = element.closest(
                    '.fc-daygrid-day'
                );

                if (dayCell) {
                    date = dayCell.getAttribute(
                        'data-date'
                    ) || '';
                }
            }

            results.push({
                date: date,
                label: text,
                Event: text
            });
        }

        return results;
    }
    """

    try:

        events = page.evaluate(
            script
        )

    except Exception as exc:

        logger.warning(
            "JavaScript event extraction failed: %s",
            exc
        )

        return []

    if not isinstance(events, list):

        return []

    logger.info(
        "Raw rendered calendar events found: %d",
        len(events)
    )

    return events


# ============================================================
# DOM FALLBACK EXTRACTION
# ============================================================

def extract_dom_events(page) -> List[Dict[str, str]]:
    """
    Fallback extraction from visible calendar DOM.
    """

    logger.info(
        "Trying DOM fallback extraction..."
    )

    events = []

    selectors = [
        ".fc-event",
        ".fc-daygrid-event",
        ".fc-event-title",
        "[class*='event']",
    ]

    for selector in selectors:

        try:

            locator = page.locator(
                selector
            )

            count = locator.count()

            if count == 0:
                continue

            logger.info(
                "Found %d elements using %s",
                count,
                selector
            )

            for index in range(count):

                try:

                    element = locator.nth(index)

                    text = (
                        element.inner_text()
                        .strip()
                    )

                    if not text:
                        continue

                    date = ""

                    try:

                        date = element.evaluate(
                            """
                            el => {
                                const parent =
                                    el.closest('[data-date]');
                                return parent
                                    ? parent.getAttribute('data-date')
                                    : '';
                            }
                            """
                        )

                    except Exception:
                        pass

                    events.append({
                        "date": date or "",
                        "label": text,
                        "Event": text,
                    })

                except Exception:
                    continue

            if events:
                break

        except Exception:
            continue

    logger.info(
        "Fallback events found: %d",
        len(events)
    )

    return events


# ============================================================
# TEXT FALLBACK
# ============================================================

def extract_calendar_text(page) -> List[Dict[str, str]]:
    """
    Last-resort extraction.

    This captures visible text from the calendar area.
    """

    logger.info(
        "Trying text-based calendar extraction..."
    )

    results = []

    container = find_calendar_container(
        page
    )

    if container is None:

        return results

    try:

        text = container.inner_text()

        if text.strip():

            lines = [
                line.strip()
                for line in text.splitlines()
                if line.strip()
            ]

            for line in lines:

                results.append({
                    "date": "",
                    "label": line,
                    "Event": line,
                })

    except Exception as exc:

        logger.warning(
            "Text extraction failed: %s",
            exc
        )

    return results


# ============================================================
# DATE NORMALIZATION
# ============================================================

def normalize_date(value: str) -> str:
    """
    Normalize dates where possible.

    Output:
        YYYY-MM-DD

    If the value cannot be parsed, the original
    value is returned.
    """

    if not value:
        return ""

    value = value.strip()

    formats = [
        "%Y-%m-%d",
        "%d-%m-%Y",
        "%d/%m/%Y",
        "%d %B %Y",
        "%d %b %Y",
        "%B %d, %Y",
        "%b %d, %Y",
    ]

    for fmt in formats:

        try:

            parsed = datetime.strptime(
                value,
                fmt
            )

            return parsed.strftime(
                "%Y-%m-%d"
            )

        except ValueError:
            continue

    return value


# ============================================================
# MONTH FILTER
# ============================================================

def filter_target_month(
    events: List[Dict[str, str]],
    target_month: str
) -> List[Dict[str, str]]:
    """
    Filter events belonging to TARGET_MONTH.
    """

    target_date = parse_target_month(
        target_month
    )

    target_year = target_date.year
    target_month_number = target_date.month

    filtered = []

    for event in events:

        date_value = normalize_date(
            event.get("date", "")
        )

        if not date_value:
            continue

        try:

            event_date = datetime.strptime(
                date_value,
                "%Y-%m-%d"
            )

        except ValueError:

            continue

        if (
            event_date.year == target_year
            and event_date.month == target_month_number
        ):

            filtered.append({
                "date": event_date.strftime(
                    "%Y-%m-%d"
                ),
                "label": event.get(
                    "label",
                    ""
                ).strip(),
                "Event": event.get(
                    "Event",
                    ""
                ).strip(),
            })

    logger.info(
        "Events after %s filtering: %d",
        target_month,
        len(filtered)
    )

    return filtered


# ============================================================
# CLEAN DATA
# ============================================================

def clean_events(
    events: List[Dict[str, str]]
) -> List[Dict[str, str]]:
    """
    Remove empty and duplicate events.
    """

    cleaned = []

    seen = set()

    for event in events:

        date = event.get(
            "date",
            ""
        ).strip()

        label = event.get(
            "label",
            ""
        ).strip()

        event_name = event.get(
            "Event",
            ""
        ).strip()

        if not date and not label and not event_name:
            continue

        key = (
            date,
            label,
            event_name,
        )

        if key in seen:
            continue

        seen.add(key)

        cleaned.append({
            "date": date,
            "label": label,
            "Event": event_name,
        })

    cleaned.sort(
        key=lambda x: (
            x["date"],
            x["label"],
            x["Event"],
        )
    )

    return cleaned


# ============================================================
# SCRAPE
# ============================================================

def _capture_debug_snapshot(page, attempt: int, blocked: bool) -> None:
    """
    Save a screenshot + HTML dump + response metadata for the current
    page state, so a zero-event result can be diagnosed later (blocked
    page vs. genuinely empty calendar vs. a structure change) without
    needing to reproduce it. Never raises: a failed debug capture must
    never mask the real error. Never writes secrets — only page URL,
    title, and rendered content.
    """

    try:
        os.makedirs(DEBUG_ARTIFACT_DIR, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        prefix = os.path.join(DEBUG_ARTIFACT_DIR, f"attempt{attempt}-{stamp}")

        page.screenshot(path=f"{prefix}.png", full_page=True)

        with open(f"{prefix}.html", "w", encoding="utf-8") as fh:
            fh.write(page.content())

        with open(f"{prefix}.txt", "w", encoding="utf-8") as fh:
            fh.write(f"URL: {page.url}\nTitle: {page.title()}\nLikely blocked: {blocked}\n")

        logger.warning(
            "Saved debug snapshot for attempt %d to %s(.png/.html/.txt). "
            "If 'Likely blocked' is True, open the .png first — it will "
            "usually show a WAF/challenge page rather than the calendar.",
            attempt,
            prefix,
        )

    except Exception as exc:
        logger.warning("Could not save debug snapshot: %s", exc)


def _scrape_shiksha_once(attempt: int) -> tuple[List[Dict[str, str]], bool]:
    """
    Run a single scrape attempt in a fresh browser session.

    Returns (events, blocked). events is [] (never raises) if the
    attempt found no usable events. blocked is True if the initial
    document request itself returned 403/429/5xx — i.e. the page never
    rendered a calendar to extract from in the first place.
    """

    with sync_playwright() as playwright:

        browser = launch_browser(
            playwright
        )

        context = create_browser_context(
            browser
        )

        page = context.new_page()

        page.set_default_timeout(
            PAGE_TIMEOUT
        )

        try:

            blocked = load_shiksha_page(
                page
            )

            wait_for_page_content(
                page
            )

            # ------------------------------------------------
            # Try FullCalendar extraction
            # ------------------------------------------------

            events = extract_fullcalendar_events(
                page
            )

            # ------------------------------------------------
            # DOM fallback
            # ------------------------------------------------

            if not events:

                logger.info(
                    "FullCalendar extraction returned no events."
                )

                events = extract_dom_events(
                    page
                )

            # ------------------------------------------------
            # Text fallback
            # ------------------------------------------------

            if not events:

                logger.info(
                    "DOM extraction returned no events."
                )

                events = extract_calendar_text(
                    page
                )

            # ------------------------------------------------
            # Filter target month
            # ------------------------------------------------

            events = filter_target_month(
                events,
                TARGET_MONTH
            )

            # ------------------------------------------------
            # Clean
            # ------------------------------------------------

            events = clean_events(
                events
            )

            if not events:

                logger.warning(
                    "No rows found for %s on attempt %d/%d.",
                    TARGET_MONTH,
                    attempt,
                    MAX_SCRAPE_ATTEMPTS,
                )

                if blocked:
                    logger.warning(
                        "Reason: the initial page request itself was "
                        "refused (403/429/5xx) — this is a source/WAF "
                        "block, not a selector or timing problem."
                    )
                else:
                    logger.warning(
                        "Possible reasons: (1) Shiksha has not published "
                        "calendar data for this month, (2) the page "
                        "structure changed, (3) calendar data now loads "
                        "from an endpoint these extractors don't cover."
                    )

                _capture_debug_snapshot(page, attempt, blocked)

                return [], blocked

            return events, blocked

        finally:

            try:
                context.close()
            except Exception:
                pass

            try:
                browser.close()
            except Exception:
                pass


def scrape_shiksha() -> List[Dict[str, str]]:
    """
    Main scraping function. Retries up to MAX_SCRAPE_ATTEMPTS times with a
    fresh browser session each time. Only returns [] (never raises) after
    every attempt is exhausted — callers (main.py) treat an empty result
    as a scrape failure and refuse to touch the Sheet/Calendar.
    """

    logger.info(
        "=================================================="
    )

    logger.info(
        "Shiksha Exam Calendar Scraper"
    )

    logger.info(
        "Target month: %s",
        TARGET_MONTH
    )

    logger.info(
        "=================================================="
    )

    was_blocked = False

    for attempt in range(1, MAX_SCRAPE_ATTEMPTS + 1):

        events, blocked = _scrape_shiksha_once(attempt)
        was_blocked = was_blocked or blocked

        if events:
            return events

        if attempt < MAX_SCRAPE_ATTEMPTS:
            delay = RETRY_BACKOFF_SECONDS * attempt
            logger.info(
                "Retrying in %d second(s) (attempt %d/%d)...",
                delay,
                attempt + 1,
                MAX_SCRAPE_ATTEMPTS,
            )
            time.sleep(delay)

    logger.warning(
        "All %d scrape attempt(s) returned zero events.",
        MAX_SCRAPE_ATTEMPTS,
    )

    if was_blocked:
        logger.warning(
            "At least one attempt was blocked at the HTTP level "
            "(403/429/5xx). Retrying from the same GitHub Actions "
            "runner will keep hitting the same source IP, so if this "
            "is an IP-reputation block, further retries here will not "
            "help — see debug_artifacts for the exact response Shiksha "
            "returned."
        )

    return []


# ============================================================
# PRINT RESULTS
# ============================================================

def print_results(
    rows: List[Dict[str, str]]
) -> None:
    """
    Print scraped rows to the GitHub Actions log.
    """

    logger.info(
        "=================================================="
    )

    logger.info(
        "SCRAPED RESULTS"
    )

    logger.info(
        "=================================================="
    )

    for index, row in enumerate(
        rows,
        start=1
    ):

        logger.info(
            "%d. %s | %s | %s",
            index,
            row.get("date", ""),
            row.get("label", ""),
            row.get("Event", ""),
        )

    logger.info(
        "=================================================="
    )


# ============================================================
# MAIN
# ============================================================

def main() -> int:

    # Tracks which stage is currently running so that, if an
    # exception is raised, the Google Chat failure notification
    # can report an accurate "Component:" name.
    current_component = "Configuration"

    try:

        # ----------------------------------------------------
        # Configuration
        # ----------------------------------------------------

        validate_configuration()

        logger.info(
            "Configuration validated."
        )

        logger.info(
            "Google Calendar is NOT used by scraper.py."
        )

        # ----------------------------------------------------
        # Scrape
        # ----------------------------------------------------

        current_component = "Scraper"

        rows = scrape_shiksha()

        # ----------------------------------------------------
        # Handle no data
        # ----------------------------------------------------

        if not rows:

            logger.warning(
                "No data found for %s.",
                TARGET_MONTH
            )

            logger.info(
                "Scraper finished without Calendar processing."
            )

            return 0

        # ----------------------------------------------------
        # Print
        # ----------------------------------------------------

        print_results(
            rows
        )

        # ----------------------------------------------------
        # Google Sheet
        # ----------------------------------------------------

        current_component = "Google Sheets"

        write_to_google_sheet(
            rows
        )

        # ----------------------------------------------------
        # SUCCESS
        # ----------------------------------------------------

        logger.info(
            "=================================================="
        )

        logger.info(
            "SCRAPER COMPLETED SUCCESSFULLY"
        )

        logger.info(
            "Rows processed: %d",
            len(rows)
        )

        logger.info(
            "Google Sheet updated."
        )

        logger.info(
            "Google Calendar was NOT accessed."
        )

        logger.info(
            "=================================================="
        )

        return 0

    except KeyboardInterrupt:

        logger.error(
            "Process interrupted by user."
        )

        return 130

    except Exception as exc:

        logger.exception(
            "Scraper failed: %s",
            exc
        )

        google_chat.send_failure_message(
            component=current_component,
            error=str(exc),
        )

        return 1


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    sys.exit(
        main()
    )
