"""Reserve membership on the API's existing workspace insertion transaction."""

from workspace_provisioning.shared_membership import reserve


async def reserve_workspace_membership(session, binding):
    # This domain helper opens no transaction and takes no session-level lock.
    # Unlike Harness admission, it explicitly REQUIRES the caller transaction;
    # borrowing its native driver keeps the workspace/grant/membership atomic.
    await session.flush()
    connection = await session.connection()
    if (
        connection.dialect.name != "postgresql"
        or connection.dialect.driver != "asyncpg"
    ):
        from workspace_provisioning.runtime_config import LifecycleRefused

        raise LifecycleRefused("shared membership requires the PostgreSQL transaction")
    raw = await connection.get_raw_connection()
    await reserve(raw.driver_connection, binding)
