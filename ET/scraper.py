"""
Shiksha Exam Calendar Scraper
=============================

Purpose:
    Scrape exam calendar data from Shiksha and write it to Google Sheets.

Google Calendar is intentionally NOT handled by this script.
Google Calendar synchronization should be handled separately by:

    calendar_sync.py

--------------------------------------------------------------------
WHY THIS VERSION IS DIFFERENT
--------------------------------------------------------------------
The previous version was failing with:

    HTTP status: 403
    server: AkamaiGHost

on the *initial document request* — before any JavaScript ran, before
any selector was queried. That means the failure was never a scraping
/ selector / timing problem. It was Akamai's bot-management layer
refusing the request outright, almost certainly because:

  1. The request "looked like" an automated headless browser
     (bundled headless Chromium has a distinct TLS/JS fingerprint,
     `navigator.webdriver` is true, missing plugins, missing
     `chrome.runtime`, etc.), and/or
  2. The source IP (a shared GitHub Actions runner IP) has a bad
     reputation with Akamai, which many high-traffic sites use to
     bulk-block datacenter ranges regardless of fingerprint.

Retrying with a fresh browser session (what the old code did) cannot
fix either cause, because the fingerprint and the source IP are the
same on every retry. This version fixes what's actually fixable and
gives you a clear, logged answer for the part that isn't:

  - Uses the real Chrome browser (`channel="chrome"`) instead of the
    bundled headless Chromium, which Akamai fingerprints far more
    aggressively.
  - Patches the standard headless "tells" (`navigator.webdriver`,
    missing plugins/languages, no `window.chrome`, permissions API
    behavior) via an init script injected before any page JS runs.
  - Sends realistic request headers (Accept, Accept-Language,
    Accept-Encoding, Sec-Fetch-*, Sec-Ch-Ua, Upgrade-Insecure-Requests)
    that match a real Chrome navigation instead of Playwright's bare
    defaults.
  - "Warms up" the session by visiting the Shiksha homepage first
    (like a real visitor would) before navigating to the exam
    calendar page, with a referer set accordingly, instead of hitting
    the deep page cold.
  - Adds human-like jitter: variable delays, a small mouse move/scroll
    before reading content.
  - Supports routing traffic through a proxy (PROXY_SERVER /
    PROXY_USERNAME / PROXY_PASSWORD env vars). If the block is IP-
    reputation based rather than fingerprint based, this is the only
    thing that will actually get you past it — no fingerprint fix can
    unblock a flagged IP.
  - Explicitly distinguishes, in the logs and in the Google Chat
    failure notification, between "still getting blocked at the HTTP
    level" (fingerprint/IP problem — needs `channel="chrome"` support
    installed and/or a proxy) vs. "page loaded but no events found"
    (a real selector/structure problem worth debugging from the saved
    HTML/screenshot).

Required environment variables:
    GOOGLE_SERVICE_ACCOUNT_JSON
    GOOGLE_SHEET_ID

Optional environment variables:
    TARGET_MONTH
    SHIKSHA_URL
    MAX_SCRAPE_ATTEMPTS
    DEBUG_ARTIFACT_DIR
    HEADLESS                 ("true"/"false", default "true")
    BROWSER_CHANNEL          (default "chrome"; falls back to bundled
                               Chromium automatically if not installed)
    PROXY_SERVER             (e.g. "http://host:port")
    PROXY_USERNAME
    PROXY_PASSWORD

Example:
    TARGET_MONTH="August 2026" python scraper.py

GitHub Actions:
    GOOGLE_SERVICE_ACCOUNT_JSON=${{ secrets.GOOGLE_SERVICE_ACCOUNT_JSON }}
    GOOGLE_SHEET_ID=${{ secrets.GOOGLE_SHEET_ID }}
    PROXY_SERVER=${{ secrets.PROXY_SERVER }}          # optional
    PROXY_USERNAME=${{ secrets.PROXY_USERNAME }}      # optional
    PROXY_PASSWORD=${{ secrets.PROXY_PASSWORD }}      # optional

IMPORTANT CI setup note:
    `channel="chrome"` requires the real Chrome binary to be present,
    not just Playwright's bundled Chromium. In your GitHub Actions
    workflow, install it with:

        playwright install chrome --with-deps

    (in addition to / instead of `playwright install chromium`). If
    Chrome isn't installed, this script automatically falls back to
    bundled Chromium and logs a warning — but bundled Chromium is the
    more easily fingerprinted option, so getting Chrome installed in
    CI is the single highest-leverage fix here.
"""

import os
import sys
import json
import re
import time
import random
import logging
from datetime import datetime
from typing import List, Dict, Optional, Tuple

import gspread
from google.oauth2.service_account import Credentials

from playwright.sync_api import (
    sync_playwright,
    TimeoutError as PlaywrightTimeoutError,
    Error as PlaywrightError,
)

import google_chat


# ============================================================
# CONFIGURATION
# ============================================================

SHIKSHA_URL = os.getenv(
    "SHIKSHA_URL",
    "https://www.shiksha.com/engineering/resources/exam-calendar"
)

SHIKSHA_HOMEPAGE_URL = os.getenv(
    "SHIKSHA_HOMEPAGE_URL",
    "https://www.shiksha.com/"
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

# ------------------------------------------------------------
# Browser / anti-detection settings
# ------------------------------------------------------------

HEADLESS = os.getenv("HEADLESS", "true").lower() == "true"

# Prefer real Chrome over bundled headless Chromium — Akamai and most
# bot-management vendors fingerprint the bundled build much more
# aggressively (different CDP surface, different JS engine build
# flags). Falls back automatically if "chrome" isn't installed.
BROWSER_CHANNEL = os.getenv("BROWSER_CHANNEL", "chrome")

PAGE_TIMEOUT = 60_000

# Random delay settings (seconds)
MIN_DELAY = 1.5
MAX_DELAY = 4.0

# A small, realistic pool of desktop Chrome user agents. Rotated per
# attempt so repeated retries don't all present an identical
# fingerprint. Kept in sync with recent Chrome versions.
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
]

# Optional proxy. If the block is IP-reputation based (very plausible
# for a shared GitHub Actions runner IP hitting a high-traffic Akamai
# -fronted site), this is the only lever that actually addresses the
# root cause — fingerprint fixes alone won't unblock a flagged IP.
PROXY_SERVER = os.getenv("PROXY_SERVER", "").strip()
PROXY_USERNAME = os.getenv("PROXY_USERNAME", "").strip()
PROXY_PASSWORD = os.getenv("PROXY_PASSWORD", "").strip()

# Retry settings: kept as a safety net for genuinely transient issues
# (a one-off 5xx, a slow load), but retries alone cannot fix a
# fingerprint or IP-reputation block — see the module docstring.
MAX_SCRAPE_ATTEMPTS = int(os.getenv("MAX_SCRAPE_ATTEMPTS", "3"))
RETRY_BACKOFF_SECONDS = 15

# Where to save a screenshot/HTML dump when a scrape attempt returns
# zero events, so a failure (blocked page vs. genuinely empty calendar
# vs. a structure change) can be diagnosed from the GitHub Actions run
# itself instead of guessing. Upload this directory as a CI artifact.
DEBUG_ARTIFACT_DIR = os.getenv("DEBUG_ARTIFACT_DIR", "debug_artifacts")

# Response codes/patterns that indicate a source/WAF-side block rather
# than a client-rendering issue.
BLOCK_STATUS_CODES = {403, 429}


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

    if PROXY_SERVER and not PROXY_SERVER.startswith(
        ("http://", "https://", "socks5://")
    ):
        raise RuntimeError(
            "PROXY_SERVER must include a scheme, e.g. "
            "'http://host:port'."
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
# PLAYWRIGHT — BROWSER / CONTEXT SETUP
# ============================================================

# Injected into every new page BEFORE any site JavaScript runs. This
# patches the standard signals bot-detection scripts check for on a
# vanilla Playwright/Puppeteer session. It cannot defeat every
# anti-bot system, but it removes the cheap, common tells.
STEALTH_INIT_SCRIPT = """
// navigator.webdriver is the single most-checked automation flag.
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

// Headless/automated sessions often have an empty plugins array.
Object.defineProperty(navigator, 'plugins', {
    get: () => [1, 2, 3, 4, 5],
});

// Real Chrome reports a non-empty languages array.
Object.defineProperty(navigator, 'languages', {
    get: () => ['en-IN', 'en-US', 'en'],
});

// window.chrome is absent on bundled headless builds by default.
window.chrome = window.chrome || { runtime: {} };

// Permissions API: automated Chrome answers "denied" for notifications
// even without a prompt shown; real Chrome answers "default".
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission })
        : originalQuery(parameters)
);

// Hide the CDP automation extension surface some detectors probe for.
Object.defineProperty(navigator, 'webdriver', { get: () => false });
"""


def build_proxy_config() -> Optional[dict]:
    """
    Build a Playwright proxy config dict from env vars, or None if no
    proxy is configured.
    """

    if not PROXY_SERVER:
        return None

    proxy_config = {"server": PROXY_SERVER}

    if PROXY_USERNAME:
        proxy_config["username"] = PROXY_USERNAME

    if PROXY_PASSWORD:
        proxy_config["password"] = PROXY_PASSWORD

    logger.info(
        "Routing browser traffic through configured proxy (%s).",
        PROXY_SERVER,
    )

    return proxy_config


def launch_browser(playwright):
    """
    Launch a real Chrome browser when available (preferred, since it
    is fingerprinted far less aggressively than bundled headless
    Chromium), falling back to bundled Chromium if the "chrome"
    channel isn't installed in this environment.
    """

    launch_kwargs = dict(
        headless=HEADLESS,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
            "--window-size=1440,900",
            "--lang=en-IN",
        ],
        proxy=build_proxy_config(),
    )

    if BROWSER_CHANNEL:

        try:

            logger.info(
                "Launching browser (channel=%s, headless=%s)...",
                BROWSER_CHANNEL,
                HEADLESS,
            )

            return playwright.chromium.launch(
                channel=BROWSER_CHANNEL,
                **launch_kwargs,
            )

        except PlaywrightError as exc:

            logger.warning(
                "Could not launch channel '%s' (%s). Falling back to "
                "bundled Chromium — note this is more easily "
                "fingerprinted by bot detection than real Chrome. "
                "Install it in CI with: "
                "`playwright install chrome --with-deps`.",
                BROWSER_CHANNEL,
                exc,
            )

    logger.info(
        "Launching bundled headless Chromium (headless=%s)...",
        HEADLESS,
    )

    return playwright.chromium.launch(**launch_kwargs)


def create_browser_context(browser):
    """
    Create a browser context configured to look like a normal desktop
    Chrome visitor from India: matching UA/sec-ch-ua headers, a
    realistic viewport, IST timezone/locale, and standard navigation
    headers that Playwright doesn't send by default.
    """

    user_agent = random.choice(USER_AGENTS)

    context = browser.new_context(
        viewport={
            "width": 1440,
            "height": 900,
        },
        locale="en-IN",
        timezone_id="Asia/Kolkata",
        user_agent=user_agent,
        extra_http_headers={
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;"
                "q=0.9,image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "en-IN,en-US;q=0.9,en;q=0.8",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
        },
    )

    context.add_init_script(STEALTH_INIT_SCRIPT)

    return context


def human_like_warmup(page) -> None:
    """
    A little bit of organic-looking activity before reading page
    content: small mouse movement + scroll. Cheap, but it means the
    session isn't 100% static input the instant the DOM is ready,
    which some behavioral detectors key on.
    """

    try:
        page.mouse.move(
            random.randint(100, 400),
            random.randint(100, 400),
        )

        page.mouse.wheel(0, random.randint(200, 600))

    except Exception:
        # Never let cosmetic jitter break the actual scrape.
        pass


# ============================================================
# PAGE LOADING
# ============================================================

def visit_homepage_first(page) -> None:
    """
    Warm up the session by visiting the Shiksha homepage before the
    deep calendar page, the way a real visitor arriving via search or
    direct navigation would. This gives the site a chance to set
    normal session cookies and avoids the (more suspicious) pattern of
    a brand-new browser session requesting a deep internal page with
    no referer as its very first action.
    """

    logger.info(
        "Warming up session via homepage [%s] ...",
        SHIKSHA_HOMEPAGE_URL,
    )

    try:

        page.goto(
            SHIKSHA_HOMEPAGE_URL,
            wait_until="domcontentloaded",
            timeout=PAGE_TIMEOUT,
        )

        human_like_warmup(page)

        random_delay()

    except Exception as exc:

        # A failed warm-up isn't fatal on its own — log it and let the
        # real navigation attempt (and its own blocked/not-blocked
        # check) be the source of truth.
        logger.warning(
            "Homepage warm-up failed (continuing anyway): %s",
            exc,
        )


def load_shiksha_page(page) -> bool:
    """
    Load the Shiksha exam calendar page.

    Returns True if the response looked like a source/WAF block (403,
    429, or 5xx on the initial navigation), False otherwise. A 403 on
    the very first request means the document itself was refused
    before any client-side JavaScript ran — no amount of waiting for
    selectors or scrolling to trigger lazy loading can produce
    calendar data in that case, because there was never a real
    calendar page in the response, only a block page.
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
            referer=SHIKSHA_HOMEPAGE_URL,
        )

        if response:
            logger.info(
                "HTTP status: %s",
                response.status
            )

            if (
                response.status in BLOCK_STATUS_CODES
                or response.status >= 500
            ):
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
                for header_name in (
                    "server",
                    "cf-ray",
                    "cf-mitigated",
                    "x-akamai-transaction-id",
                    "x-iinfo",
                ):
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

    human_like_warmup(page)

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
# TEXT-BASED DATE EXTRACTION (fallback when no date attribute exists)
# ============================================================

# The August 14 run confirmed Shiksha's calendar page is NOT built on
# FullCalendar (0 raw FullCalendar events every attempt) and does not
# expose a `data-date` attribute anywhere near its event elements (12
# DOM-fallback events found, 0 survived date filtering — every one had
# an empty date). That means the date almost certainly lives in the
# *visible text* of each event ("15 Aug 2026 - JEE Advanced Result"
# style), not in an attribute. This module extracts it from there.

_MONTH_NAMES = (
    "January|February|March|April|May|June|July|August|September|"
    "October|November|December|"
    "Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
)

# Ordered by specificity. Each pattern requires an explicit 4-digit
# year in the matched text — dates without a year are deliberately not
# guessed at, since silently assuming TARGET_MONTH's year for a date
# that didn't actually state one risks mis-filing events into the
# wrong month/year rather than just dropping them.
_DATE_TEXT_PATTERNS = [
    # 2026-08-15
    re.compile(r"\b(\d{4}-\d{1,2}-\d{1,2})\b"),
    # 15/08/2026 or 15-08-2026
    re.compile(r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{4})\b"),
    # 15th August 2026 / 15 Aug 2026 / 15 Aug, 2026
    re.compile(
        rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH_NAMES})\.?,?\s+(\d{{4}})\b",
        re.IGNORECASE,
    ),
    # August 15, 2026 / Aug 15 2026
    re.compile(
        rf"\b({_MONTH_NAMES})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b",
        re.IGNORECASE,
    ),
]

_MONTH_LOOKUP = {
    name[:3].lower(): index
    for index, name in enumerate(
        [
            "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November",
            "December",
        ],
        start=1,
    )
}
_MONTH_LOOKUP["sept"] = 9


def extract_date_from_text(text: str) -> str:
    """
    Search free text for a date that includes an explicit 4-digit
    year, and normalize it to YYYY-MM-DD. Returns "" if no confident
    match is found — this deliberately does not guess a year, so an
    event whose text has no year is left for a human to look at (via
    the debug snapshot) rather than silently mis-filed.
    """

    if not text:
        return ""

    for pattern in _DATE_TEXT_PATTERNS:

        match = pattern.search(text)

        if not match:
            continue

        groups = match.groups()

        try:

            if len(groups) == 1:
                # ISO or D/M/Y-style single captured token — let
                # normalize_date's strptime table handle it.
                candidate = normalize_date(groups[0])

                if candidate and candidate != groups[0]:
                    return candidate

                # normalize_date returned the value unchanged, meaning
                # none of its formats matched (e.g. D/M/Y ambiguity) —
                # try both day-first and month-first before giving up.
                raw = groups[0].replace("-", "/")

                for fmt in ("%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d"):
                    try:
                        return datetime.strptime(
                            raw, fmt
                        ).strftime("%Y-%m-%d")
                    except ValueError:
                        continue

                continue

            if len(groups) == 3 and groups[0].isdigit():
                # (day, month name, year)
                day, month_name, year = groups
                month_num = _MONTH_LOOKUP.get(month_name[:4].lower())
                if month_num is None:
                    month_num = _MONTH_LOOKUP.get(month_name[:3].lower())

            else:
                # (month name, day, year)
                month_name, day, year = groups
                month_num = _MONTH_LOOKUP.get(month_name[:4].lower())
                if month_num is None:
                    month_num = _MONTH_LOOKUP.get(month_name[:3].lower())

            if month_num is None:
                continue

            return datetime(
                int(year), month_num, int(day)
            ).strftime("%Y-%m-%d")

        except (ValueError, KeyError):
            continue

    return ""


# JS-side attribute probe shared by both extraction paths: checks a
# much wider set of date-ish attributes than just FullCalendar's
# `data-date`, plus any nested/ancestor <time datetime="..."> element,
# since custom (non-FullCalendar) calendar widgets commonly use one of
# these instead.
_DATE_ATTR_PROBE_JS = """
el => {
    const dateAttrs = [
        'data-date', 'data-day', 'data-start', 'data-startdate',
        'data-event-date', 'data-eventdate', 'data-datetime',
    ];

    for (const attr of dateAttrs) {
        const withAttr = el.closest(`[${attr}]`);
        if (withAttr) {
            const value = withAttr.getAttribute(attr);
            if (value) return value;
        }
    }

    const timeEl = el.querySelector('time[datetime]')
        || el.closest('time[datetime]')
        || (el.closest('[class*="event"]')
            && el.closest('[class*="event"]').querySelector('time[datetime]'));

    if (timeEl) {
        const value = timeEl.getAttribute('datetime');
        if (value) return value;
    }

    return '';
}
"""


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

    script = (
        """
    (dateAttrProbe) => {
        const results = [];
        const probeFn = new Function('el', 'return (' + dateAttrProbe + ')(el)');

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

            const dayCell = element.closest('.fc-daygrid-day');
            if (dayCell) {
                date = dayCell.getAttribute('data-date') || '';
            }

            if (!date) {
                try {
                    date = probeFn(element) || '';
                } catch (e) {
                    date = '';
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
    )

    try:

        events = page.evaluate(
            script,
            _DATE_ATTR_PROBE_JS,
        )

    except Exception as exc:

        logger.warning(
            "JavaScript event extraction failed: %s",
            exc
        )

        return []

    if not isinstance(events, list):

        return []

    # Text-based fallback for anything the attribute probe missed —
    # see extract_date_from_text()'s docstring for why this only fires
    # when the label text itself contains an explicit year.
    for event in events:
        if not event.get("date"):
            event["date"] = extract_date_from_text(
                event.get("label", "")
            )

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

                        date = element.evaluate(_DATE_ATTR_PROBE_JS)

                    except Exception:
                        pass

                    date = date or ""

                    # Text-based fallback — see extract_date_from_text()'s
                    # docstring for why this requires an explicit year
                    # in the text rather than guessing one.
                    if not date:
                        date = extract_date_from_text(text)

                    events.append({
                        "date": date,
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
                    "date": extract_date_from_text(line),
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

def _capture_debug_snapshot(
    page,
    attempt: int,
    blocked: bool,
    raw_events: Optional[List[Dict[str, str]]] = None,
) -> None:
    """
    Save a screenshot + HTML dump + a short metadata file for the
    current page state, so a zero-event result can be diagnosed later
    (blocked page vs. genuinely empty calendar vs. a structure change)
    without needing to reproduce it. Never raises: a failed debug
    capture must never mask the real error. Never writes secrets —
    only the page URL, title, and rendered content.

    If raw_events is given (the events found BEFORE month-filtering),
    they're also dumped to a .json file. This is what makes a
    zero-after-filtering result diagnosable from the run log/artifact
    alone: it shows exactly what label/date each scraped element had,
    so a wrong date-extraction guess can be fixed precisely on the
    next pass instead of re-guessing blind.
    """

    try:
        os.makedirs(DEBUG_ARTIFACT_DIR, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        prefix = os.path.join(
            DEBUG_ARTIFACT_DIR, f"attempt{attempt}-{stamp}"
        )

        page.screenshot(path=f"{prefix}.png", full_page=True)

        with open(f"{prefix}.html", "w", encoding="utf-8") as fh:
            fh.write(page.content())

        with open(f"{prefix}.txt", "w", encoding="utf-8") as fh:
            fh.write(
                f"URL: {page.url}\n"
                f"Title: {page.title()}\n"
                f"Likely blocked: {blocked}\n"
            )

        if raw_events is not None:
            with open(f"{prefix}.raw_events.json", "w", encoding="utf-8") as fh:
                json.dump(raw_events, fh, indent=2, ensure_ascii=False)

        logger.warning(
            "Saved debug snapshot for attempt %d to %s"
            "(.png/.html/.txt%s). If 'Likely blocked' is True, check "
            "the .png first — it will usually show a WAF/challenge "
            "page rather than the calendar. Otherwise check "
            ".raw_events.json to see exactly what date/label each "
            "scraped element had before filtering.",
            attempt,
            prefix,
            "/.raw_events.json" if raw_events is not None else "",
        )

    except Exception as exc:
        logger.warning("Could not save debug snapshot: %s", exc)


def _scrape_shiksha_once(attempt: int) -> Tuple[List[Dict[str, str]], bool]:
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

            # Visit the homepage first so the session looks like an
            # organic visit rather than a cold hit on a deep page.
            visit_homepage_first(page)

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
            # Log what was actually scraped BEFORE filtering, so a
            # zero-after-filtering result is diagnosable from the run
            # log alone instead of requiring a screenshot every time.
            # ------------------------------------------------

            raw_events = events

            logger.info(
                "Raw events before %s filtering: %d",
                TARGET_MONTH,
                len(raw_events),
            )

            for raw_event in raw_events[:20]:
                logger.info(
                    "  raw: date=%r label=%r",
                    raw_event.get("date", ""),
                    (raw_event.get("label", "") or "")[:80],
                )

            if len(raw_events) > 20:
                logger.info(
                    "  ... and %d more raw event(s) (see "
                    ".raw_events.json in the debug snapshot for the "
                    "full list if this attempt ends up with 0 rows)",
                    len(raw_events) - 20,
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
                        "block, not a selector or timing problem. If "
                        "this persists after switching to "
                        "channel=\"chrome\" with stealth patches, the "
                        "block is most likely IP-reputation based — "
                        "configure PROXY_SERVER to route around it."
                    )
                elif raw_events:
                    logger.warning(
                        "Reason: %d element(s) were scraped but NONE "
                        "had a date this scraper could confidently "
                        "extract for %s. Check the 'raw:' lines above "
                        "(or .raw_events.json in the debug snapshot) "
                        "to see the actual label text Shiksha is "
                        "rendering — that will show exactly what date "
                        "format/markup needs to be added to "
                        "extract_date_from_text() or the attribute "
                        "probe in _DATE_ATTR_PROBE_JS.",
                        len(raw_events),
                        TARGET_MONTH,
                    )
                else:
                    logger.warning(
                        "Possible reasons: (1) Shiksha has not "
                        "published calendar data for this month, "
                        "(2) the page structure changed, (3) calendar "
                        "data now loads from an endpoint these "
                        "extractors don't cover."
                    )

                _capture_debug_snapshot(page, attempt, blocked, raw_events)

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
    Main scraping function. Retries up to MAX_SCRAPE_ATTEMPTS times with
    a fresh browser session each time. This remains useful for
    genuinely transient issues (a one-off 5xx, a slow load), but note
    that it will NOT fix a fingerprint-based or IP-reputation-based
    block on its own — see the module docstring for what actually
    addresses that (channel="chrome" + stealth patches + optional
    proxy). Only returns [] (never raises) after every attempt has
    been exhausted.
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
        "Browser channel: %s | Headless: %s | Proxy configured: %s",
        BROWSER_CHANNEL or "bundled chromium",
        HEADLESS,
        bool(PROXY_SERVER),
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
            "(403/429/5xx) even with stealth patches and a real "
            "Chrome channel. This strongly suggests an IP-reputation "
            "block on the GitHub Actions runner IP rather than a "
            "fingerprint issue — retrying from the same runner will "
            "keep hitting the same source IP. Configure PROXY_SERVER "
            "(and PROXY_USERNAME/PROXY_PASSWORD if needed) to route "
            "around it, or run this job from a non-datacenter IP."
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
