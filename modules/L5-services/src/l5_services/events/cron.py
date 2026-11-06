"""Bounded, UTC five-field numeric cron semantics for the shared repair owner."""

from datetime import UTC, datetime, timedelta
import re


def fields(expression):
    if not isinstance(expression, str) or len(expression) > 255:
        raise ValueError("schedule requires five numeric UTC cron fields")
    parts = expression.split()
    if len(parts) != 5:
        raise ValueError("schedule requires five numeric UTC cron fields")
    result = []
    for part, (low, high) in zip(parts, ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7)), strict=True):
        values = set()
        for item in part.split(","):
            match = re.fullmatch(r"(\*|[0-9]+(?:-[0-9]+)?)(?:/([0-9]+))?", item)
            if match is None:
                raise ValueError("unsupported cron field")
            base, step = match.groups()
            step = int(step or 1)
            if not 1 <= step <= high - low + 1:
                raise ValueError("invalid cron step")
            if base == "*":
                start, end = low, high
            elif "-" in base:
                start, end = map(int, base.split("-"))
            else:
                start = int(base)
                end = high if match.group(2) else start
            if not low <= start <= end <= high:
                raise ValueError("invalid cron range")
            values.update(range(start, end + 1, step))
        result.append(values)
    result[-1] = {value % 7 for value in result[-1]}
    return parts, result


def _day_matches(day, parts, values):
    dom, dow = day.day in values[2], (day.weekday() + 1) % 7 in values[4]
    # Standard cron: restricted day-of-month/day-of-week fields are alternatives.
    return (dom and dow) if parts[2].startswith("*") or parts[4].startswith("*") else (dom or dow)


def next_tick(expression, at):
    """First matching minute at/after at, with an eight-year search bound."""
    if not isinstance(at, datetime) or at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("schedule clock must have a timezone")
    parts, values = fields(expression)
    at = at.astimezone(UTC)
    floor = at.replace(second=0, microsecond=0)
    at = floor if at == floor else floor + timedelta(minutes=1)
    day = at.replace(hour=0, minute=0)
    for _ in range(366 * 8):
        if day.month in values[3] and _day_matches(day, parts, values):
            for hour in sorted(values[1]):
                for minute in sorted(values[0]):
                    candidate = day.replace(hour=hour, minute=minute)
                    if candidate >= at:
                        return candidate
        day += timedelta(days=1)
    raise ValueError("schedule has no reachable UTC tick")


def matches(expression, tick):
    return next_tick(expression, tick) == tick.astimezone(UTC)
