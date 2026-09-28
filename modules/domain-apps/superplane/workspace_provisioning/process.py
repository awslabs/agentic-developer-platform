"""Private worker processes and synchronous bootstrap's owning-loop SQL bridge."""

import asyncio
from contextlib import contextmanager
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import time

from .runtime_config import LifecycleRefused


class AsyncBridgeStore:
    """Run only on the bootstrap thread; connections stay on their owning loop."""

    def __init__(self, connect, loop):
        self.connect, self.loop = connect, loop
        self.connection = None
        self.owner = None
        self.depth = 0

    def wait(self, future):
        call = asyncio.run_coroutine_threadsafe(future, self.loop)
        try:
            return call.result(timeout=30)
        except BaseException:
            call.cancel()
            raise

    @contextmanager
    def transaction(self):
        async def start():
            if self.connection is None:
                self.owner = self.connect()
                self.connection = await self.owner.__aenter__()
            transaction = self.connection.transaction()
            await transaction.start()
            return transaction

        transaction = self.wait(start())
        self.depth += 1
        try:
            yield
        except BaseException:
            self.wait(transaction.rollback())
            raise
        else:
            self.wait(transaction.commit())
        finally:
            self.depth -= 1
            if not self.depth:

                async def close():
                    try:
                        await self.owner.__aexit__(None, None, None)
                    finally:
                        self.connection = self.owner = None

                self.wait(close())

    def execute(self, statement, parameters):
        names = []

        def bind(match):
            name = match[1]
            if name not in parameters:
                raise LifecycleRefused("bootstrap SQL parameter is missing")
            if name not in names:
                names.append(name)
            return "$" + str(names.index(name) + 1)

        sql = re.sub(r"(?<!:):([a-zA-Z_][a-zA-Z_0-9]*)", bind, statement)

        async def execute():
            if self.connection is not None:
                return await self.connection.fetch(
                    sql, *(parameters[name] for name in names)
                )
            async with self.connect() as connection:
                return await connection.fetch(
                    sql, *(parameters[name] for name in names)
                )

        return [dict(row) for row in self.wait(execute())]


class WorkerProcesses:
    """A reviewed executable and explicit role credentials; no ambient CLI state."""

    def __init__(self, *, binaries, directory, session, region, verify):
        self.binaries, self.directory = binaries, Path(directory)
        self.session, self.region, self.verify = session, region, verify

    def run(self, argv, *, data=None, timeout=120, cwd=None):
        command = list(argv)
        supplied = data.encode() if isinstance(data, str) else data
        if supplied is not None and (
            not isinstance(supplied, bytes) or len(supplied) > 1 << 20
        ):
            raise LifecycleRefused("worker command input exceeded its bound")
        selected = next(
            (
                name
                for name, path in self.binaries.items()
                if command[0] in {name, path}
            ),
            None,
        )
        if selected is None:
            raise LifecycleRefused("worker executable is outside the reviewed set")
        command[0] = self.binaries[selected]
        self.verify()
        credentials = self.session.get_credentials()
        if credentials is None:
            raise LifecycleRefused("operation-bound AWS credentials are unavailable")
        frozen = credentials.get_frozen_credentials()
        # This process-local environment never modifies the agent host's HOME,
        # AWS profile, Terraform workspace or kubeconfig. It is built from scratch.
        environment = {
            "PATH": os.pathsep.join(
                dict.fromkeys(str(Path(path).parent) for path in self.binaries.values())
            ),
            "HOME": str(self.directory),
            "LANG": "C.UTF-8",
            "AWS_CONFIG_FILE": os.devnull,
            "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
            "AWS_EC2_METADATA_DISABLED": "true",
            "AWS_PAGER": "",
            "AWS_MAX_ATTEMPTS": "1",
            "AWS_RETRY_MODE": "standard",
            "AWS_REGION": self.region,
            "AWS_DEFAULT_REGION": self.region,
            "AWS_ACCESS_KEY_ID": frozen.access_key,
            "AWS_SECRET_ACCESS_KEY": frozen.secret_key,
            "AWS_SESSION_TOKEN": frozen.token or "",
            "TF_IN_AUTOMATION": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        started = time.monotonic()
        checked_at = started
        with (
            tempfile.TemporaryFile(dir=self.directory) as stdout,
            tempfile.TemporaryFile(dir=self.directory) as stderr,
            tempfile.TemporaryFile(dir=self.directory) as stdin,
        ):
            if supplied is not None:
                stdin.write(supplied)
                stdin.seek(0)
            process = subprocess.Popen(
                command,
                cwd=cwd or self.directory,
                env=environment,
                stdin=stdin if supplied is not None else subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
            )
            try:
                while process.poll() is None:
                    if time.monotonic() - checked_at >= 2:
                        self.verify()
                        checked_at = time.monotonic()
                    if time.monotonic() - started >= timeout:
                        raise LifecycleRefused("worker provider command timed out")
                    if (
                        max(
                            os.fstat(stdout.fileno()).st_size,
                            os.fstat(stderr.fileno()).st_size,
                        )
                        > 16 << 20
                    ):
                        raise LifecycleRefused(
                            "worker provider response exceeded its bound"
                        )
                    time.sleep(0.25)
                self.verify()
                stdout.seek(0)
                stderr.seek(0)
                return subprocess.CompletedProcess(
                    tuple(argv),
                    process.returncode,
                    stdout.read(16 << 20).decode(),
                    stderr.read(16 << 20).decode(),
                )
            except BaseException:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                if process.poll() is None:
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=5)
                # A parent can exit before its Terraform child; terminate the
                # entire owned process group even in that case.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                raise

    def checked(self, argv, *, cwd=None, timeout=120):
        result = self.run(argv, cwd=cwd, timeout=timeout)
        if result.returncode:
            raise LifecycleRefused("reviewed lifecycle command refused or failed")
        return result.stdout
