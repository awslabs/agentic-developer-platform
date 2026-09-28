"""Shared Task host errors without importing worker transport or credentials."""


class TaskRunClientError(Exception):
    """A task-scoped operation was unavailable or refused."""
