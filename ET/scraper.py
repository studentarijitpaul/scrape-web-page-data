"""
Shiksha Exam Calendar Scraper
Uses Playwright + FullCalendar's own JS API to get the full event list,
then filters to the requested month and writes the data to a Google Sheet.



Usage:
    python scraper.py                    # August 2026 (default)
    python scraper.py "September 2026"   # any month/year



Google Sheets setup (one-time):
    1. Create a Google Cloud project and enable the Google Sheets API.
    2. Create a Service Account, download its JSON key.
    3. Either add the JSON key as a Replit Secret named
       GOOGLE_SERVICE_ACCOUNT_JSON and set GOOGLE_SHEET_ID, or create a local
       google_sheets_config.json file from google_sheets_config.example.json.
    5. Share the sheet with the service account's email (editor access).
"""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

URL = "https://www.shiksha.com/engineering/resources/exam-calendar"
TARGET_MONTH = "August 2026"
GOOGLE_SHEETS_CONFIG_FILE = "google_sheets_config.json"
# Regex to validate the month argument (e.g. "August 2026")
_MONTH_RE = re.compile(
    r"^(January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+\d{4}$"
)
_MONTH_NUMS = {
    "January": 1, "February": 2, "March": 3, "April": 4,
    "May": 5, "June": 6, "July": 7, "August": 8,
    "September": 9, "October": 10, "November": 11, "December": 12,
}


def _find_chromium() -> str | None:
    """Return the path to a system Chromium binary, or None to use Playwright's own."""
    path = shutil.which("chromium") or shutil.which("chromium-browser")
    if not path:
        try:
            path = subprocess.check_output(["which", "chromium"], text=True).strip()
        except Exception:
            path = None
    return path or None


def _strip_html(html: str) -> str:
    """Very light HTML → plain-text conversion."""
    text = re.sub(r"<[^>]+>", " ", html)
    text = (text.replace("&nbsp;", " ")
                .replace("&amp;", "&")
                .replace("&lt;", "<")
                .replace("&gt;", ">")
                .replace("&quot;", '"'))
    return re.sub(r"\s+", " ", text).strip()


def scrape_calendar(month_label: str = TARGET_MONTH) -> list[dict]:
    """
    Open the Shiksha exam-calendar in a headless browser, pull all events
    from FullCalendar's internal event store via the jQuery API, and return
    those that fall within `month_label` (e.g. "August 2026").
    """
    if not _MONTH_RE.match(month_label):
        print(
            f"ERROR: month label must be like 'August 2026' — got '{month_label}'"
        )
        sys.exit(1)

    month_name, year_str = month_label.split()
    year = int(year_str)
    month_num = _MONTH_NUMS[month_name]
    prefix = f"{year:04d}-{month_num:02d}-"   # e.g. "2026-08-"

    print(f"Shiksha Exam Calendar Scraper — target: {month_label}\n")
    print("Launching headless browser …")

    with sync_playwright() as p:
        chromium_path = _find_chromium()
        launch_kwargs: dict = {"headless": True}
        if chromium_path:
            launch_kwargs["executable_path"] = chromium_path

        browser = p.chromium.launch(**launch_kwargs)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            locale="en-US",
        )
        page = context.new_page()

        print(f"Fetching {URL} …")
        try:
            page.goto(URL, timeout=60_000, wait_until="networkidle")
        except PlaywrightTimeout:
            print("ERROR: Page load timed out.")
            browser.close()
            return []

        # Give FullCalendar time to render its current month, then navigate
        # to the target month if needed.
        print("Waiting for FullCalendar to initialise …")
        try:
            page.wait_for_selector(".fc-view-container", timeout=20_000)
        except PlaywrightTimeout:
            print("WARNING: FullCalendar container not found in 20 s.")

        page.wait_for_timeout(2_000)

        # Navigate the calendar to the target month so FullCalendar loads
        # events for it into its event store.
        print(f"Navigating calendar to {month_label} …")
        _navigate_to_month(page, month_name, year)

        page.wait_for_timeout(2_000)

        # Pull all events via FullCalendar's jQuery API.
        print("Extracting events from FullCalendar …")
        raw_events: list[dict] = page.evaluate("""() => {
            try {
                const fc = jQuery('.fc');
                if (!fc.length) return [];
                const evts = fc.fullCalendar('clientEvents');
                return evts.map(e => ({
                    exam:        String(e.fullTitle  || e.title       || '').trim(),
                    event_desc:  String(e.fullDescription || e.description || '').trim(),
                    start:       e.start ? e.start.format('YYYY-MM-DD') : null,
                    end:         e.end   ? e.end.format('YYYY-MM-DD')   : null,
                    event_type:  String(e.eventType || '').trim(),
                    exam_url:    String(e.exam_url   || e.article_url || '').trim(),
                }));
            } catch(ex) {
                return [];
            }
        }""")

        browser.close()

    # Filter to the requested month and clean up HTML in descriptions.
    rows = []
    for ev in raw_events:
        start = ev.get("start") or ""
        # Include events that START in the target month.
        if not start.startswith(prefix):
            continue
        rows.append({
            "date":       start,
            "exam":       _strip_html(ev.get("exam", "")),
            "event":      _strip_html(ev.get("event_desc", "")),
            "event_type": _strip_html(ev.get("event_type", "")),
            "exam_url":   ev.get("exam_url", ""),
        })

    # Sort by date then exam name for readability.
    rows.sort(key=lambda r: (r["date"], r["exam"]))
    return rows


def _navigate_to_month(page, month_name: str, year: int) -> None:
    """
    Click FullCalendar's prev/next buttons until the calendar header shows
    the target month/year (e.g. 'August 2026').
    """
    target = f"{month_name} {year}"
    for _ in range(36):      # safety cap: at most 3 years of navigation
        try:
            header_text = page.text_content("h2.fc-header-toolbar, .fc-center h2", timeout=3_000)
        except Exception:
            header_text = page.evaluate(
                "() => document.querySelector('.fc-center h2')?.textContent || ''"
            )
        if header_text and header_text.strip() == target:
            return

        # Decide direction: parse current month to see if we're before or after.
        cur = _parse_fc_header(header_text or "")
        if cur is None:
            break
        cur_year, cur_month = cur
        tgt_month = _MONTH_NUMS[month_name]
        go_next = (year, tgt_month) > (cur_year, cur_month)
        btn_sel = ".fc-next-button" if go_next else ".fc-prev-button"
        try:
            page.click(btn_sel, timeout=3_000)
            page.wait_for_timeout(800)
        except Exception:
            break


def _parse_fc_header(text: str):
    """Parse 'August 2026' → (2026, 8) or None."""
    m = re.match(
        r"(January|February|March|April|May|June|July|August|September|"
        r"October|November|December)\s+(\d{4})",
        text.strip(),
    )
    if not m:
        return None
    return int(m.group(2)), _MONTH_NUMS[m.group(1)]


def _load_google_sheets_config() -> tuple[str | None, str | None]:
    """Load credentials from environment variables or a local ignored JSON file."""
    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    sheet_id = os.environ.get("GOOGLE_SHEET_ID")

    config_path = Path(GOOGLE_SHEETS_CONFIG_FILE)
    if sa_json and sheet_id or not config_path.exists():
        return sa_json, sheet_id

    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"\n⚠  {GOOGLE_SHEETS_CONFIG_FILE} is not valid JSON: {e}")
        return sa_json, sheet_id

    if not sa_json and config.get("service_account"):
        sa_json = json.dumps(config["service_account"])
    if not sheet_id:
        sheet_id = config.get("google_sheet_id")
    return sa_json, sheet_id


def save_to_google_sheet(rows: list[dict], month_label: str) -> None:
    """
    Write rows to a Google Sheet tab named after the month.
    Reads credentials from either environment variables / secrets:
      GOOGLE_SERVICE_ACCOUNT_JSON  — full JSON key of the service account
      GOOGLE_SHEET_ID              — the spreadsheet ID from its URL
    or google_sheets_config.json (which must not be committed to Git).
    """
    sa_json, sheet_id = _load_google_sheets_config()

    if not sa_json:
        print("\n⚠  Google service-account credentials not set — skipping Google Sheets upload.")
        return
    if not sheet_id:
        print("\n⚠  Google Sheet ID not set — skipping Google Sheets upload.")
        return

    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError:
        print("\n⚠  gspread / google-auth not installed — skipping Google Sheets upload.")
        return

    print("\nConnecting to Google Sheets …")
    try:
        creds_dict = json.loads(sa_json)
    except json.JSONDecodeError as e:
        print(f"⚠  GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON: {e}")
        return

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive.file",
    ]
    try:
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(sheet_id)
    except Exception as e:
        print(f"⚠  Could not open Google Sheet: {e}")
        return

    # Create or clear the tab for this month.
    tab_name = month_label  # e.g. "August 2026"
    try:
        ws = sh.worksheet(tab_name)
        ws.clear()
        print(f"  Cleared existing tab '{tab_name}'.")
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=tab_name, rows=500, cols=5)
        print(f"  Created new tab '{tab_name}'.")

    # Write header + data rows.
    header = ["Date", "Exam", "Event", "Event Type", "Exam URL"]
    data = [header] + [
        [r["date"], r["exam"], r["event"], r["event_type"], r["exam_url"]]
        for r in rows
    ]
    ws.update(data, value_input_option="RAW")
    sheet_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}"
    print(f"✓  Wrote {len(rows)} rows to '{tab_name}' tab.")
    print(f"   Sheet: {sheet_url}")


def print_table(rows: list[dict]) -> None:
    if not rows:
        return
    header = f"{'Date':<12} {'Exam':<22} {'Event'}"
    print("\n" + header)
    print("-" * max(len(header), 80))
    for r in rows:
        exam = r["exam"][:21]
        event = r["event"][:55]
        print(f"{r['date']:<12} {exam:<22} {event}")


def main():
    month = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else TARGET_MONTH
    rows = scrape_calendar(month)

    if not rows:
        print(
            "\nNo rows found for the requested month. Possible reasons:\n"
            "  • The calendar doesn't have data for that month yet.\n"
            "  • The site blocked the request.\n"
            "  • The month label doesn't match (e.g. 'August 2026').\n"
        )
        sys.exit(1)

    print(f"\nFound {len(rows)} exam event(s) for '{month}'.")
    print_table(rows)
    print()
    save_to_google_sheet(rows, month)


if __name__ == "__main__":
    main()
