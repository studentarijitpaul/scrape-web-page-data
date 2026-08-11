"""
google_chat.py
===============

Sends automation status notifications to a Google Chat space using an
incoming webhook.

Why a webhook (and not the Google Chat API / a Chat app)?
    This project only ever needs to *push* a notification into a space -
    it never needs to read messages, respond to users, or act as an
    interactive bot. A Chat space's incoming webhook is purpose-built for
    exactly that: it is a single per-space URL that accepts an HTTP POST
    with a JSON body, requires no OAuth flow, no service-account scopes,
    and no extra Google Cloud API to enable. That makes it the simplest
    secure option for a GitHub Actions job, and it reuses nothing from -
    and adds no risk to - the existing Sheets/Calendar service-account
    credentials.

Required environment variable:
    GOOGLE_CHAT_WEBHOOK_URL
        The full incoming-webhook URL for the target Chat space.
        Never hard-code this value. Provide it via a local .env file
        (for local testing) or a GitHub Actions secret (in CI).

Design notes:
    * This module fails *soft*. A missing webhook URL, a network error,
      or a bad response from Google Chat is logged and returns False -
      it never raises. A broken Chat notification must never be the
      reason the scrape/Sheets/Calendar pipeline is marked as failed.
    * Only the standard library is used (urllib), since no functionality
      here requires the `requests` package.
    * The webhook URL and full payload are never logged, to avoid
      leaking a working webhook URL (or its token/key) into CI logs.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Optional

logger = logging.getLogger("google_chat")

# ============================================================
# CONFIGURATION
# ============================================================

WEBHOOK_URL_ENV_VAR = "GOOGLE_CHAT_WEBHOOK_URL"

REQUEST_TIMEOUT_SECONDS = 10

# Retries are only used for transient failures (429 / 5xx / network
# errors). Non-retryable errors (400/401/403/404) fail immediately.
MAX_RETRIES = 3
RETRY_BACKOFF_BASE_SECONDS = 2


# ============================================================
# INTERNAL HELPERS
# ============================================================

def _get_webhook_url() -> Optional[str]:
    """
    Read the webhook URL from the environment.

    Returns None (rather than raising) if it is not configured, so that
    Google Chat notifications are always optional and never break the
    scraper / Sheets / Calendar pipeline.
    """

    webhook_url = os.environ.get(WEBHOOK_URL_ENV_VAR, "").strip()

    if not webhook_url:
        logger.warning(
            "[GOOGLE CHAT] %s is not set - skipping notification.",
            WEBHOOK_URL_ENV_VAR,
        )
        return None

    return webhook_url


def _post_message(text: str) -> bool:
    """
    POST a simple text message to the configured Chat webhook.

    Retries on 429 / 5xx / network errors with exponential backoff.
    Never logs the webhook URL, the payload, or raw response bodies
    that might echo back the URL.
    """

    webhook_url = _get_webhook_url()

    if not webhook_url:
        return False

    payload = json.dumps({"text": text}).encode("utf-8")

    last_error: Optional[BaseException] = None

    for attempt in range(1, MAX_RETRIES + 1):

        request = urllib.request.Request(
            webhook_url,
            data=payload,
            headers={"Content-Type": "application/json; charset=UTF-8"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(
                request,
                timeout=REQUEST_TIMEOUT_SECONDS,
            ) as response:

                status = response.getcode()

                if 200 <= status < 300:
                    logger.info(
                        "[GOOGLE CHAT] Notification sent successfully."
                    )
                    return True

                logger.error(
                    "[GOOGLE CHAT] Unexpected status code: %s",
                    status,
                )
                return False

        except urllib.error.HTTPError as exc:

            last_error = exc

            if exc.code in (429, 500, 502, 503, 504):

                delay = RETRY_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))

                logger.warning(
                    "[GOOGLE CHAT] Temporary error (HTTP %s) on "
                    "attempt %d/%d. Retrying in %d second(s)...",
                    exc.code,
                    attempt,
                    MAX_RETRIES,
                    delay,
                )

                time.sleep(delay)
                continue

            if exc.code == 400:
                logger.error(
                    "[GOOGLE CHAT] HTTP 400 Bad Request - the message "
                    "payload was rejected. Check the message format."
                )
            elif exc.code == 401:
                logger.error(
                    "[GOOGLE CHAT] HTTP 401 Unauthorized - the webhook "
                    "URL is invalid, malformed, or missing its token."
                )
            elif exc.code == 403:
                logger.error(
                    "[GOOGLE CHAT] HTTP 403 Forbidden - the webhook may "
                    "have been disabled, or the app/space was removed."
                )
            elif exc.code == 404:
                logger.error(
                    "[GOOGLE CHAT] HTTP 404 Not Found - the webhook URL "
                    "does not point to a valid Chat space webhook."
                )
            else:
                logger.error(
                    "[GOOGLE CHAT] HTTP %s error while sending "
                    "notification.",
                    exc.code,
                )

            return False

        except urllib.error.URLError as exc:

            last_error = exc

            delay = RETRY_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))

            logger.warning(
                "[GOOGLE CHAT] Network error on attempt %d/%d: %s. "
                "Retrying in %d second(s)...",
                attempt,
                MAX_RETRIES,
                getattr(exc, "reason", exc),
                delay,
            )

            time.sleep(delay)

        except Exception as exc:  # noqa: BLE001 - must never crash caller

            logger.error(
                "[GOOGLE CHAT] Unexpected error sending notification: "
                "%s",
                type(exc).__name__,
            )
            return False

    logger.error(
        "[GOOGLE CHAT] Failed to send notification after %d attempt(s). "
        "Last error: %s",
        MAX_RETRIES,
        type(last_error).__name__ if last_error else "unknown",
    )

    return False


def _timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


# ============================================================
# PUBLIC API
# ============================================================

def send_success_message(
    new_events: int = 0,
    updated_events: int = 0,
    skipped_events: int = 0,
    removed_events: int = 0,
) -> bool:
    """
    Notify the Chat space that the full pipeline (Sheets + Calendar)
    completed successfully, including a breakdown of what changed.
    """

    lines = [
        "✅ Exam Calendar Automation Completed",
        "",
        "📊 Google Sheets: Updated successfully",
        "📅 Google Calendar: Updated successfully",
        "💬 Google Chat: Notification sent successfully",
        "",
        f"New events: {new_events}",
        f"Updated events: {updated_events}",
        f"Skipped events: {skipped_events}",
    ]

    if removed_events:
        lines.append(f"Removed events: {removed_events}")

    lines.append("")
    lines.append(f"Execution time: {_timestamp()}")

    return _post_message("\n".join(lines))


def send_no_changes_message(skipped_events: int = 0) -> bool:
    """
    Notify the Chat space that the pipeline ran successfully but there
    was nothing new to sync. Kept separate from send_success_message so
    a "quiet" run doesn't read like something meaningful happened, and
    so callers can choose not to send it at all if they'd rather stay
    silent on no-op runs.
    """

    text = (
        "ℹ️ Exam Calendar — No changes detected.\n\n"
        f"Checked: {skipped_events} event(s)\n"
        f"Execution time: {_timestamp()}"
    )

    return _post_message(text)


def send_failure_message(component: str, error: str) -> bool:
    """
    Notify the Chat space that a component of the pipeline failed.

    `component` should be a short, human-readable label such as
    "Scraper", "Google Sheets", or "Google Calendar".
    """

    # Keep the message reasonably short and never include anything that
    # looks like it could be a credential (the caller is expected to
    # pass str(exception), not raw env/config dumps).
    error_snippet = (error or "Unknown error").strip()[:500]

    text = (
        "❌ Exam Calendar Automation Failed\n\n"
        f"Component: {component}\n"
        f"Error: {error_snippet}\n"
        f"Time: {_timestamp()}"
    )

    return _post_message(text)


# ============================================================
# MANUAL / LOCAL TEST ENTRY POINT
# ============================================================

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    print("Sending a test message to Google Chat...")

    ok = send_success_message(
        new_events=3,
        updated_events=1,
        skipped_events=12,
    )

    if ok:
        print("Test message sent. Check your Google Chat space.")
    else:
        print(
            "Test message was NOT sent. See the log output above - "
            f"most likely {WEBHOOK_URL_ENV_VAR} is not set."
        )
