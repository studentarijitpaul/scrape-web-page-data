# Shiksha August Calendar Scraper — VS Code + clasp setup

This is a Google Apps Script project. It can be *edited* in VS Code, but it
*runs* on Google's servers (it needs `SpreadsheetApp` / `UrlFetchApp`, which
only exist there) — `clasp` is Google's official bridge between the two.

## One-time setup

1. Install Node.js if you don't already have it (clasp needs it).
2. Install clasp globally:
   ```
   npm install -g @google/clasp
   ```
3. Log in (opens a browser window to authorize your Google account):
   ```
   clasp login
   ```
4. In this folder, create a new Apps Script project bound to a fresh Google Sheet:
   ```
   clasp create --type sheets --title "Shiksha Exam Calendar"
   ```
   This generates a `.clasp.json` file linking this folder to that new project.

   (Alternative: if you already made a Sheet + Apps Script project via the
   web editor, run `clasp clone <scriptId>` instead — the scriptId is in the
   Apps Script editor's URL.)

## Every time you edit code

1. Edit `Code.js` in VS Code as normal.
2. Push your changes to Google:
   ```
   clasp push
   ```
3. Run the function remotely:
   ```
   clasp run scrapeAugustCalendar
   ```
   (First `clasp run` may ask you to enable the Apps Script API at
   script.google.com/home/usersettings — it'll give you a direct link.)
4. Check logs without leaving VS Code:
   ```
   clasp logs
   ```
5. Open the actual Sheet in a browser to see the "August 2026" tab:
   ```
   clasp open
   ```

## If scrapeAugustCalendar finds nothing

That means the calendar table is rendered client-side by JavaScript after
page load, and `UrlFetchApp` (server-side fetch, no JS execution) can't see
it — same limitation `IMPORTHTML` has. `clasp logs` will show a message
saying the month label wasn't found in the raw HTML, confirming this. At
that point the only real fix is a headless browser (Playwright/Selenium),
which is what your original Colab script was attempting — and that's the
one hitting Shiksha's anti-bot wall.
