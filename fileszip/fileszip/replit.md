# Shiksha Exam Calendar Scraper

A Python scraper that pulls exam-calendar data from Shiksha and saves it to a CSV file.
It uses a headless Chromium browser (Playwright) to handle the JS-rendered page content.

## Stack
- Python 3.12
- Playwright (headless Chromium)

## How to run

```
python scraper.py
```

By default it targets **August 2026**. Pass a different month as an argument:

```
python scraper.py "September 2026"
```

Output is printed to the console and saved to `exam_calendar.csv`.

## First-time setup (already done)

```
pip install -r requirements.txt
playwright install chromium
```

## Files

| File | Purpose |
|------|---------|
| `scraper.py` | Main scraper — fetch, parse, output |
| `requirements.txt` | Python dependencies |
| `exam_calendar.csv` | Generated output (created on first run) |
| `Code.js` / `appsscript.json` | Original Google Apps Script version (kept for reference) |

## Notes

- If the scraper finds no rows, the site may have changed its markup or is blocking
  automated requests. Check `playwright` debug output or try with `headless=False`
  locally.
- The month label must match exactly (e.g. `"August 2026"`).

## User preferences
