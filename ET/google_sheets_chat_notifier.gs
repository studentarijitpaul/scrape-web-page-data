/**
 * Sends a Google Chat message whenever a person edits this spreadsheet.
 *
 * Install this as an installable "On edit" trigger in the Google Sheet's
 * Apps Script project. Do not use a simple onEdit trigger: UrlFetchApp
 * requires the authorization granted to an installable trigger.
 *
 * Store the Google Chat incoming-webhook URL in the Apps Script project
 * setting named GOOGLE_CHAT_WEBHOOK_URL. Keeping it in Script Properties
 * prevents the secret being committed with this file.
 */

const WEBHOOK_PROPERTY = 'GOOGLE_CHAT_WEBHOOK_URL';
const MAX_PREVIEW_ROWS = 5;
const MAX_PREVIEW_COLUMNS = 5;

/**
 * Installable trigger entry point. Google invokes this after a user edits
 * a cell, pastes a range, or clears a range in the bound spreadsheet.
 */
function notifyGoogleChatOnEdit(event) {
  if (!event || !event.range) {
    throw new Error('This function must be run by an installable On edit trigger.');
  }

  const webhookUrl = PropertiesService.getScriptProperties()
    .getProperty(WEBHOOK_PROPERTY);
  if (!webhookUrl) {
    throw new Error(`Missing Apps Script property: ${WEBHOOK_PROPERTY}`);
  }

  const range = event.range;
  const sheet = range.getSheet();
  const preview = formatRangePreview_(range);
  const editor = event.user && event.user.getEmail
    ? event.user.getEmail()
    : 'A spreadsheet user';

  const lines = [
    'Google Sheet updated',
    '',
    `Sheet: ${sheet.getName()}`,
    `Cell range: ${range.getA1Notation()}`,
    `Changed by: ${editor}`,
  ];

  if (preview) {
    lines.push('', 'Current value(s):', preview);
  }

  const response = UrlFetchApp.fetch(webhookUrl, {
    method: 'post',
    contentType: 'application/json',
    payload: JSON.stringify({text: lines.join('\n')}),
    muteHttpExceptions: true,
  });

  if (response.getResponseCode() < 200 || response.getResponseCode() >= 300) {
    throw new Error(`Google Chat webhook returned HTTP ${response.getResponseCode()}.`);
  }
}

function formatRangePreview_(range) {
  const values = range
    .getDisplayValues()
    .slice(0, MAX_PREVIEW_ROWS)
    .map(row => row.slice(0, MAX_PREVIEW_COLUMNS));

  if (!values.length) return '';

  const text = values
    .map(row => row.map(value => value || '(blank)').join(' | '))
    .join('\n');
  const omittedRows = range.getNumRows() - values.length;
  const omittedColumns = range.getNumColumns() - (values[0] || []).length;
  const suffix = omittedRows > 0 || omittedColumns > 0
    ? '\n(Preview truncated)'
    : '';

  return text + suffix;
}
