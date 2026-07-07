from datetime import datetime, timezone, timedelta


def parse_published_at(value):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    if isinstance(value, (int, float)):
        timestamp = int(value)
        if abs(timestamp) > 10**12:
            timestamp = timestamp / 1000
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)

    if isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            raise ValueError("Empty timestamp value")
        if normalized.isdigit():
            timestamp = int(normalized)
            if abs(timestamp) > 10**12:
                timestamp = timestamp / 1000
            return datetime.fromtimestamp(timestamp, tz=timezone.utc)

        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"

        return datetime.fromisoformat(normalized).astimezone(timezone.utc)

    raise TypeError(f"Unsupported publishedAt value: {value!r}")


def time_check(value, delta_hours: int) -> bool:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=delta_hours)
    return value >= cutoff


def parse_opportunity_published(value):
    """Shared parser for the standardized opportunity shape used by every domain.

    Accepts all the formats individual scrapers emit (ISO datetime strings,
    ISO date strings, and unix timestamps in seconds or milliseconds) and
    returns an aware ``datetime`` in UTC, or ``None`` when the value is empty
    or unparseable. Returns ``None`` (not raise) so callers can decide whether
    to keep rows with unknown publish dates.
    """
    if not value:
        return None
    try:
        if isinstance(value, (int, float)):
            return parse_published_at(value)
        # ISO date strings (YYYY-MM-DD) are fine too — fromisoformat handles them.
        return parse_published_at(str(value))
    except (TypeError, ValueError):
        return None
