from change_detector import detect_changes, filter_allowed_exams, generate_exam_id, load_allowed_names, normalize_exam_name, should_notify


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
