/**
 * Shiksha Exam Calendar -> August 2026 rows -> Google Sheet
 *
 * HOW TO USE:
 * 1. Open (or create) a Google Sheet.
 * 2. Extensions > Apps Script.
 * 3. Delete the boilerplate code, paste this whole file in.
 * 4. Click Run (with function `scrapeAugustCalendar` selected) once to
 *    authorize permissions.
 * 5. Check the sheet - a tab called "August 2026" will be created/filled.
 *
 * OPTIONAL: to run this automatically on a schedule, go to the clock icon
 * (Triggers) on the left sidebar > Add Trigger > choose `scrapeAugustCalendar`,
 * time-driven, daily. Fully free, no Colab/servers needed.
 */

const URL = "https://www.shiksha.com/engineering/resources/exam-calendar";
const TARGET_MONTH_LABEL = "August 2026";   // change this to reuse for other months
const SHEET_TAB_NAME = "August 2026";

function scrapeAugustCalendar() {
  const html = fetchPageHtml_(URL);

  if (!html) {
    Logger.log("Could not fetch page HTML at all - request may have been blocked.");
    return;
  }

  const rows = extractMonthRows_(html, TARGET_MONTH_LABEL);

  if (rows.length === 0) {
    Logger.log("No rows found for " + TARGET_MONTH_LABEL + ". " +
      "The page may be JS-rendered (data loaded after initial HTML), " +
      "in which case UrlFetchApp (like IMPORTHTML) won't see it either - " +
      "you'd need a headless-browser based approach instead.");
    return;
  }

  writeRowsToSheet_(rows);
  Logger.log("Done. Wrote " + rows.length + " rows to tab '" + SHEET_TAB_NAME + "'.");
}

/**
 * Fetches raw HTML for a URL. Returns null on failure instead of throwing,
 * so the caller can log a clear message.
 */
function fetchPageHtml_(url) {
  const options = {
    method: "get",
    muteHttpExceptions: true,
    followRedirects: true,
    headers: {
      "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
      "Accept-Language": "en-US,en;q=0.9"
    }
  };

  try {
    const response = UrlFetchApp.fetch(url, options);
    const code = response.getResponseCode();
    if (code !== 200) {
      Logger.log("Non-200 response: " + code);
      return null;
    }
    return response.getContentText();
  } catch (e) {
    Logger.log("Fetch error: " + e);
    return null;
  }
}

/**
 * Very lightweight HTML table parser scoped to a specific month section.
 * Looks for the target month heading text, then parses the <table> that
 * follows it, up to the next month heading (or end of document).
 *
 * NOTE: This uses regex against raw HTML rather than a real DOM parser
 * (Apps Script has no built-in HTML DOM). It's intentionally simple and
 * may need small tweaks if Shiksha changes their markup - if it stops
 * matching, log the raw `html` variable and inspect the tags around the
 * month heading / table to adjust the regex patterns below.
 */
function extractMonthRows_(html, monthLabel) {
  const rows = [];

  // Find where this month's heading appears in the raw HTML.
  const monthIndex = html.indexOf(monthLabel);
  if (monthIndex === -1) {
    Logger.log("Month label '" + monthLabel + "' not found anywhere in fetched HTML.");
    return rows;
  }

  // Look for the next month-like heading after this one, to know where to stop.
  const monthPattern = /(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4}/g;
  monthPattern.lastIndex = monthIndex + monthLabel.length;
  const nextMatch = monthPattern.exec(html);
  const sectionEnd = nextMatch ? nextMatch.index : html.length;

  const section = html.substring(monthIndex, sectionEnd);

  // Extract table rows within this section.
  const rowMatches = section.match(/<tr[\s\S]*?<\/tr>/gi) || [];

  rowMatches.forEach(function (rowHtml) {
    const cellMatches = rowHtml.match(/<td[\s\S]*?<\/td>/gi);
    if (!cellMatches || cellMatches.length < 3) return; // skip header/short rows

    const cells = cellMatches.map(function (cellHtml) {
      return stripTags_(cellHtml).trim();
    });

    rows.push({
      date: cells[0],
      label: cells[1],
      event: cells[2]
    });
  });

  return rows;
}

function stripTags_(htmlFragment) {
  return htmlFragment
    .replace(/<[^>]*>/g, " ")   // remove tags
    .replace(/&nbsp;/g, " ")
    .replace(/&amp;/g, "&")
    .replace(/\s+/g, " ")
    .trim();
}

function writeRowsToSheet_(rows) {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = ss.getSheetByName(SHEET_TAB_NAME);
  if (!sheet) {
    sheet = ss.insertSheet(SHEET_TAB_NAME);
  } else {
    sheet.clear();
  }

  sheet.appendRow(["Date", "Label", "Event"]);
  rows.forEach(function (r) {
    sheet.appendRow([r.date, r.label, r.event]);
  });

  sheet.autoResizeColumns(1, 3);
}
