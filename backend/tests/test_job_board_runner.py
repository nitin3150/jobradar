from datetime import datetime, timedelta, timezone

from utils.time_check import parse_published_at
from pipeline.nodes.jobs_boards.runner import compute_since_cutoff


def test_parse_published_at_handles_millisecond_timestamps():
    value = 1772551058051

    parsed = parse_published_at(value)

    assert parsed == datetime(2026, 3, 1, 0, 0, 58, 51, tzinfo=timezone.utc)


def test_compute_since_cutoff_uses_the_latest_previous_scan():
    now = datetime(2026, 7, 5, 12, 0, 0, tzinfo=timezone.utc)
    last_run = now - timedelta(minutes=30)

    cutoff = compute_since_cutoff(now=now, delta_hours=1, last_run=last_run)

    assert cutoff == last_run
