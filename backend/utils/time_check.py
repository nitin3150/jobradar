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