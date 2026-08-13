from change_detector import detect_changes, filter_allowed_exams, generate_exam_id, load_allowed_names, normalize_exam_name, should_notify
from calendar_sync import deduplicate_rows_by_event_id
from google_sheets import deduplicate_sheet_rows


def row(date="2026-08-20", exam="CAT", event="CAT exam", event_type="CAT exam"):
    return {"date": date, "exam": exam, "event": event, "event_type": event_type, "exam_url": ""}


def test_normalization_and_duplicate_allowlist_entries():
    assert normalize_exam_name("  C\u00a0A T  ") == "c a t"
    assert load_allowed_names([" CAT ", "cat", "", "CAT"]) == {"cat"}


def test_filtering_rejects_not_allowed_exam():
    assert filter_allowed_exams([row(exam="CAT"), row(exam="XAT")], {"cat"}) == [row(exam="CAT")]


def test_change_classification_for_new_unchanged_updated_and_removed():
    previous = [row(), row(exam="XAT")]
    current = [row(date="2026-08-25"), row(exam="CMAT")]
    changes = detect_changes(previous, current)
    assert len(changes["new"]) == 1
    assert len(changes["updated"]) == 1
    assert len(changes["removed"]) == 1
    assert not changes["unchanged"]


def test_stable_id_excludes_date_and_only_changes_notify():
    assert generate_exam_id(row("2026-08-20")) == generate_exam_id(row("2026-08-25"))
    assert should_notify("new") and should_notify("updated")
    assert not should_notify("unchanged") and not should_notify("removed")


def test_calendar_rows_with_the_same_stable_id_are_collapsed():
    assert deduplicate_rows_by_event_id([row("2026-08-01"), row("2026-08-02")]) == [row("2026-08-01")]


def test_one_exam_is_kept_per_date_even_when_source_metadata_differs():
    first = row("2026-08-01")
    duplicate = row("2026-08-01")
    duplicate["exam_url"] = "https://example.test/variant"
    different_date = row("2026-08-02")
    assert deduplicate_sheet_rows([first, duplicate, different_date]) == [first, different_date]
