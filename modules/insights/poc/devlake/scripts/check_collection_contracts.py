"""Verify pinned model DDL, worker connection code and real isolated PostgreSQL."""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[5]
POC = ROOT / 'modules/insights/poc/devlake'


def main():
    build_path = ROOT / 'modules/connectors/devlake/scripts/build.py'
    spec = importlib.util.spec_from_file_location('devlake_contract_build', build_path)
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    lock = builder.read_lock(builder.ROOT / 'source-lock.json')
    if lock['toolchain'] not in subprocess.check_output(['go', 'version'], text=True).split():
        raise ValueError('contract checks require the pinned Go toolchain')
    with tempfile.TemporaryDirectory(prefix='devlake-contract-') as directory:
        root = Path(directory)
        source = root / 'source'
        builder.checkout(lock, source)
        for patch in lock['patches']:
            subprocess.run(['git', '-C', str(source), 'apply', '--unidiff-zero', str(builder.ROOT / 'patches' / patch['path'])], check=True)
        backend = source / 'backend'
        shutil.copytree(POC / 'schema-models', backend / 'cmd/adp-schema-models')
        shutil.copytree(POC / 'cmd/collector-worker', backend / 'cmd/adp-collector-worker')
        shutil.copytree(ROOT / 'modules/connectors/devlake/sourceapi', backend / 'cmd/adp-collector-worker/internal/sourceapi')
        shutil.copytree(ROOT / 'modules/connectors/devlake/normalization', backend / 'adp/normalization')
        environment = {**os.environ, 'GOTOOLCHAIN': 'local'}
        actual = subprocess.check_output(['go', 'run', '-mod=readonly', './cmd/adp-schema-models'], cwd=backend, env=environment, text=True)
        sql = (POC / 'migrations/schema-v2.sql').read_text()
        if actual.strip() != sql[sql.index('-- BEGIN PINNED'):sql.index('-- END PINNED') + len('-- END PINNED DEVLAKE MODELS')].strip():
            raise ValueError('migration columns differ from the pinned upstream models')
        subprocess.run(['go', 'test', '-mod=readonly', './cmd/adp-collector-worker/internal/...', '-count=1'], cwd=backend, env=environment, check=True)
        binary = root / 'collection.test'
        subprocess.run(['go', 'test', '-mod=readonly', '-c', '-o', str(binary), './cmd/adp-collector-worker/internal/collection'], cwd=backend, env=environment, check=True)
        environment['DEVLAKE_S03_COLLECTION_TEST_BINARY'] = str(binary)
        worker_binary = root / 'worker.test'
        subprocess.run(['go', 'test', '-mod=readonly', '-c', '-o', str(worker_binary), './cmd/adp-collector-worker'], cwd=backend, env=environment, check=True)
        environment['DEVLAKE_S03_WORKER_TEST_BINARY'] = str(worker_binary)
        native_binary = root / 'native-worker.test'
        subprocess.run(['go', 'test', '-mod=readonly', '-c', '-o', str(native_binary), './cmd/adp-collector-worker/internal/worker'], cwd=backend, env=environment, check=True)
        environment['DEVLAKE_S03_NATIVE_WORKER_TEST_BINARY'] = str(native_binary)
        if importlib.util.find_spec('pgserver') is not None:
            import pgserver
            server = pgserver.get_server(root / 'pgdata', cleanup_mode='delete')
        else:
            from embedded_postgres.postgres_server import PostgresServer
            server = PostgresServer(root / 'pgdata')
            server.ensure_pgdata_inited()
            server.ensure_postgres_running()
        try:
            environment['DEVLAKE_POSTGRES_DSN'] = server.get_uri()
            subprocess.run([sys.executable, '-m', 'unittest', '-v', 'modules.insights.tests.devlake_poc.test_collection_schema_postgres'], cwd=ROOT, env=environment, check=True)
        finally:
            server.cleanup()


if __name__ == '__main__':
    main()
