"""Use native synchronous cleanup SQL on the Gateway's asyncpg connection.

The worker thread blocks on the owner event loop; no second connection or retry
can conceal an unknown commit. Only maintained native statements reach this
adapter. Transaction ownership and the registration advisory lock are retained.
"""

import asyncio
import re
from contextlib import contextmanager
from threading import get_ident


class NativeStore:
    def __init__(self, connection, loop):
        self.connection, self.loop = connection, loop
        self.owner = get_ident()
        self.active = False

    def wait(self, coroutine):
        if get_ident() != self.owner:
            coroutine.close()
            raise RuntimeError("cleanup SQL called from a different thread")
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result()

    @contextmanager
    def transaction(self):
        if self.active:
            raise RuntimeError("unexpected nested cleanup SQL transaction")
        tx = self.connection.transaction()
        self.wait(tx.start())
        self.active = True
        try:
            yield
        except BaseException:
            try:
                self.wait(tx.rollback())
            except Exception:
                pass  # Preserve original uncertainty; never reconnect.
            raise
        else:
            self.wait(tx.commit())
        finally:
            self.active = False

    def execute(self, statement, parameters):
        names = []

        def parameter(match):
            name = match[1]
            if name not in names:
                names.append(name)
            return "$" + str(names.index(name) + 1)

        sql = re.sub(r"(?<!:):([a-zA-Z_][a-zA-Z_0-9]*)", parameter, statement)
        return [dict(row) for row in self.wait(self.connection.fetch(sql, *(parameters[name] for name in names)))]
