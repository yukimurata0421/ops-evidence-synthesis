from __future__ import annotations

from datetime import UTC, datetime


def parse_timestamp(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = value.strip()
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def format_timestamp(value: str | datetime) -> str:
    parsed = parse_timestamp(value)
    return parsed.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def utc_now() -> str:
    return format_timestamp(datetime.now(UTC))
