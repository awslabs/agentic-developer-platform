"""One-shot privileged preparation; this module is never imported by the API.

Inputs arrive only through a temporary operator-created Secret. The maintained
installer supplies ownership-checking SQL and the exact role/password mapping.
No credentials or raw database exceptions are printed.
"""

import asyncio
import json
import os
import re
import ssl
from urllib.parse import urlsplit

import asyncpg


async def prepare():
    url = os.environ["SUPERPLANE_PREPARATION_ADMIN_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)
    parsed = urlsplit(url)
    expected = json.loads(os.environ["SUPERPLANE_PREPARATION_TARGET"])
    if (parsed.scheme != "postgresql" or parsed.hostname != expected["host"]
        or (parsed.port or 5432) != expected["port"] or parsed.path != "/" + expected["database"]
        or parsed.query or parsed.fragment):
        raise ValueError("Preparation administrator targets a different database")
    passwords = json.loads(os.environ["SUPERPLANE_PREPARATION_PASSWORDS"])
    roles = {f"superplane_{expected['environment']}_{kind}" for kind in ("runtime", "migration", "skypilot")}
    if set(passwords) != roles or not all(re.fullmatch(r"superplane_[a-z][a-z0-9-]{0,39}_(runtime|migration|skypilot)", role) for role in roles):
        raise ValueError("Preparation role identity mismatch")
    sql = os.environ["SUPERPLANE_PREPARATION_SQL"]
    if not sql.endswith("COMMIT;\n"):
        raise ValueError("Transactional preparation script required")
    connection = await asyncpg.connect(url, ssl=ssl.create_default_context(cadata=os.environ["SUPERPLANE_DATABASE_CA"]), timeout=15)
    try:
        await apply_preparation(connection, sql, passwords)
    finally:
        await connection.close()
    return {"status": "prepared", "database": expected["database"], "roles": sorted(roles)}


async def apply_preparation(connection, sql, passwords):
    try:
        # Hold the generator's transaction/advisory lock across role creation
        # and password assignment; no half-prepared role set is committed.
        await connection.execute(sql.removesuffix("COMMIT;\n"))
        for role, password in passwords.items():
            if not isinstance(password, str) or len(password) < 32:
                raise ValueError("Strong role password required")
            statement = await connection.fetchval("SELECT format('ALTER ROLE %I PASSWORD %L', $1::text, $2::text)", role, password)
            await connection.execute(statement)
        await connection.execute("COMMIT")
    except BaseException:
        await connection.execute("ROLLBACK")
        raise


def main():
    try:
        print(json.dumps(asyncio.run(prepare())))
        return 0
    except Exception:
        print(json.dumps({"status": "refused", "reason": "Database preparation failed; credentials and database errors are redacted"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
