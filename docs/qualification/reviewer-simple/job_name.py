"""Correct base-branch example for the isolated reviewer conflict qualification."""


def normalize_job_name(value: str) -> str:
    """Trim surrounding whitespace while preserving case and internal spacing."""
    return value.strip()
