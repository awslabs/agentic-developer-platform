"""Operator-configurable, separate browser budgets (seconds)."""

import os


def seconds(name, default, maximum=1800):
    value = int(os.environ.get(name, default))
    if not 1 <= value <= maximum:
        raise ValueError(f"{name} must be between 1 and {maximum} seconds")
    return value


STARTUP_SECONDS = seconds("CYBER_BROWSER_STARTUP_SECONDS", 120)
ACTION_SECONDS = seconds("CYBER_BROWSER_ACTION_SECONDS", 90)
NAVIGATION_SECONDS = seconds("CYBER_BROWSER_NAVIGATION_SECONDS", 45, 120)
SCREENSHOT_SECONDS = seconds("CYBER_BROWSER_SCREENSHOT_SECONDS", 5, 30)
LEASE_SECONDS = seconds("CYBER_BROWSER_SESSION_SECONDS", 600)
