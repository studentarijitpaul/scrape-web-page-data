"""Deterministic filtering and change detection for the exam sync."""
from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from typing import Iterable


def normalize_exam_name(value: str) -> str:
    """Return a conservative canonical form suitable for exact matching."""
    value = html.unescape(str(value or ""))
    value = unicodedata.normalize("NFKC", value).casefold()
    value = re.sub(r"[\s\-_./,;:()\[\]{}]+", " ", value)
    return " ".join(value.split())


def load_allowed_names(values: Iterable[str]) -> set[str]:
    return {name for value in values if (name := normalize_exam_name(value))}


def filter_allowed_exams(rows: Iterable[dict], allowed_names: set[str]) -> list[dict]:
    """Keep rows whose extracted exam name exactly matches the allowlist."""
    return [row for row in rows if normalize_exam_name(row.get("exam", "")) in allowed_names]


def generate_exam_id(row: dict) -> str:
    """Stable identity: exam name plus event type, deliberately excluding date."""
    key = "|".join((normalize_exam_name(row.get("exam", "")), normalize_exam_name(row.get("event_type", ""))))
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def detect_changes(previous: Iterable[dict], current: Iterable[dict]) -> dict[str, list]:
    """Classify current data relative to the previously synchronized sheet."""
    old = {generate_exam_id(row): row for row in previous}
    new = {generate_exam_id(row): row for row in current}
    result = {"new": [], "updated": [], "unchanged": [], "removed": []}
    for identity, row in new.items():
        old_row = old.get(identity)
        if old_row is None:
            result["new"].append(row)
        elif _comparable(old_row) == _comparable(row):
            result["unchanged"].append(row)
        else:
            result["updated"].append({"previous": old_row, "current": row})
    result["removed"] = [row for identity, row in old.items() if identity not in new]
    return result


def _comparable(row: dict) -> tuple:
    return tuple(str(row.get(key, "")).strip() for key in ("date", "exam", "event", "event_type", "exam_url"))


def should_notify(change: str) -> bool:
    return change in {"new", "updated"}
