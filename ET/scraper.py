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

def load_shiksha_page(page) -> None:
    """
    Load the Shiksha exam calendar page.
    """

    logger.info(
        "Fetching [%s] ...",
        SHIKSHA_URL
    )

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

        random_delay()

    except PlaywrightTimeoutError:

        logger.warning(
            "Page load timed out. "
            "Continuing because the page may still be usable."
        )

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

def scrape_shiksha() -> List[Dict[str, str]]:
    """
    Main scraping function.
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

            load_shiksha_page(
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
                    "No rows found for %s.",
                    TARGET_MONTH
                )

                logger.warning(
                    "Possible reasons:"
                )

                logger.warning(
                    "1. Shiksha has not published "
                    "calendar data for this month."
                )

                logger.warning(
                    "2. Website structure has changed."
                )

                logger.warning(
                    "3. Calendar data is loaded "
                    "dynamically through another API."
                )

                logger.warning(
                    "4. The website blocked the request."
                )

                # Do not fail the GitHub workflow merely
                # because no calendar events were found.
                return []

            return events

        finally:

            try:
                context.close()
            except Exception:
                pass

            try:
                browser.close()
            except Exception:
                pass


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
