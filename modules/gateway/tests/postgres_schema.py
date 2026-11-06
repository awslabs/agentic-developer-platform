"""Session-owned migrated PostgreSQL templates for opt-in business fixtures."""

import hashlib
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path

import psycopg2


def source_signature(gateway: Path) -> str:
    """Invalidate templates when migration code or its local imports change."""
    digest = hashlib.sha256()
    roots = [
        gateway / "alembic",
        gateway / "src",
        gateway.parent / "L5-services/src",
        gateway.parents[1] / "libs/python/adp-common",
    ]
    paths = [gateway / "alembic.ini", gateway / "pyproject.toml"]
    for root in roots:
        paths.extend(sorted(root.rglob("*.py")))
    for path in paths:
        digest.update(str(path.relative_to(gateway.parents[1])).encode() + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()


class PostgresSchemas:
    def __init__(self, server, gateway, database_factory, upgrade):
        self.server = server
        self.gateway = gateway
        self.database_factory = database_factory
        self.upgrade = upgrade
        self.templates = {}
        self.owned = ExitStack()

    @contextmanager
    def database(self):
        signature = source_signature(self.gateway)
        name = self.templates.get(signature)
        if name is None:
            # Commit ownership to the session only after migration succeeds.
            # A failed build drops its database immediately and cannot poison a cache.
            with ExitStack() as building:
                url = building.enter_context(self.database_factory(self.server))
                self.upgrade(url)
                with closing(psycopg2.connect(url)) as connection, connection, connection.cursor() as cursor:
                    cursor.execute("SELECT current_database()")
                    name = cursor.fetchone()[0]
                # No test may connect to or mutate the source database. PostgreSQL
                # still permits CREATE DATABASE ... TEMPLATE while connections are off.
                from psycopg2 import sql

                with closing(psycopg2.connect(self.server.get_uri())) as connection, connection, connection.cursor() as cursor:
                    cursor.execute(sql.SQL("ALTER DATABASE {} ALLOW_CONNECTIONS false").format(sql.Identifier(name)))
                self.owned.enter_context(building.pop_all())
                self.templates[signature] = name
        with self.database_factory(self.server, template=name) as url:
            yield url

    def close(self):
        self.owned.close()
        self.templates.clear()
