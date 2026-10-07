"""One-shot operator process, supplied only to an isolated preparation pod."""

import asyncio
import json
import os
import ssl
from urllib.parse import urlsplit

import asyncpg

from harness_jobs.installation import prepare
from harness_jobs.schema import check_schema_version


async def run():
    inputs = json.loads(os.environ["ADP_OPERATION_DATABASE_INPUT"])
    request = inputs["request"]
    tls = ssl.create_default_context(cadata=inputs["ca_pem"])
    target = inputs["target"]
    for dsn in [inputs["admin_url"], *inputs["runtime_dsns"].values()]:
        parsed = urlsplit(dsn)
        if (
            parsed.scheme != "postgresql"
            or parsed.hostname != target["host"]
            or (parsed.port or 5432) != target["port"]
            or parsed.path != "/" + request["database"]
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Database target differs")
    connection = await asyncpg.connect(inputs["admin_url"], ssl=tls, timeout=15)
    try:
        result = await prepare(connection, request, inputs["passwords"])
    finally:
        await connection.close()
    # Prove the persisted credentials work with TLS, independently of the
    # administrator transaction. A lost response is reconciled by replay.
    for role, dsn in inputs["runtime_dsns"].items():
        connection = await asyncpg.connect(
            dsn,
            ssl=tls,
            timeout=15,
            server_settings={"search_path": request["schema"] + ",pg_catalog"},
        )
        try:
            if await connection.fetchval("SELECT current_user") != role:
                raise ValueError("Runtime identity differs")
            await check_schema_version(connection)
        finally:
            await connection.close()
    result["roles_authenticated"] = True
    return result


try:
    print(json.dumps(asyncio.run(run())))
except Exception:
    print(
        json.dumps(
            {
                "status": "refused",
                "reason": "Shared database preparation failed; credentials and database errors are redacted",
            }
        )
    )
    raise SystemExit(2) from None
