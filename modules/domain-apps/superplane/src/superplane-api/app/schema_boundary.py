"""One schema setting shared by API sessions and the maintained Alembic chain."""

import os
import re
import ssl


def schema_name(value: str) -> str | None:
    if not value:
        return None
    if (
        not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", value)
        or value in {"public", "information_schema"}
        or value.startswith("pg_")
    ):
        raise ValueError("SUPERPLANE_DB_SCHEMA must name one isolated domain schema")
    return value


def connect_args(value: str) -> dict:
    schema = schema_name(value)
    result = {"server_settings": {"search_path": schema}} if schema else {}
    ca = os.environ.get("SUPERPLANE_DATABASE_CA")
    if ca:
        # An explicit context pins server trust and hostname verification for
        # asyncpg without incompatible libpq URL parameters.
        result["ssl"] = ssl.create_default_context(cadata=ca)
    return result
