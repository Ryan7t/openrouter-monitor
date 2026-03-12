from __future__ import annotations

import hashlib
from datetime import date, datetime, time, timedelta


def format_currency(amount: float | None) -> str:
    if amount is None:
        return "无限制"
    return f"${amount:.2f}"


def format_local_datetime(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S %Z")


def iso_or_none(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def dedupe_expired(last_notified_at: datetime | None, dedupe_hours: int, now: datetime) -> bool:
    if last_notified_at is None:
        return True
    return now - last_notified_at >= timedelta(hours=dedupe_hours)


def hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def mask_api_key(api_key: str) -> str:
    if len(api_key) > 24:
        return f"{api_key[:17]}.....{api_key[-7:]}"
    if len(api_key) > 10:
        return f"{api_key[:6]}.....{api_key[-4:]}"
    prefix_length = max(1, len(api_key) // 2)
    suffix_length = max(1, len(api_key) - prefix_length)
    return f"{api_key[:prefix_length]}.....{api_key[-suffix_length:]}"


def parse_time_value(value: str) -> time:
    parts = value.split(":")
    if len(parts) != 2:
        raise ValueError("must use HH:MM format")
    hour, minute = (int(part) for part in parts)
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("must use a valid 24-hour time")
    return time(hour=hour, minute=minute)


def format_hhmm(value: time) -> str:
    return value.strftime("%H:%M")


def parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None
