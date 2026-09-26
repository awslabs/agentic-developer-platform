"""Real CodeGraph CLI/storage boundary; executed only in an offline container."""

import errno
import importlib.metadata
import json
import os
import subprocess
import sys
from pathlib import Path


def command(*args, success=True):
    result = subprocess.run(args, check=False, text=True, capture_output=True, timeout=45)
    if success:
        assert result.returncode == 0, result.stdout + result.stderr
    else:
        assert result.returncode != 0, 'expected denied command unexpectedly succeeded'
    return result.stdout + result.stderr


def refused_write(path):
    try:
        with path.open('ab'):
            pass
    except OSError as exc:
        assert exc.errno in (errno.EROFS, errno.EACCES, errno.EPERM), exc
    else:
        raise AssertionError(f'protected path is writable: {path}')


def main():
    assert os.getuid() == 1001 and os.getgid() == 1001
    assert Path.home() == Path('/data'), 'CGC_HOME alone does not configure upstream storage'
    status = Path('/proc/self/status').read_text()
    for expected in ['NoNewPrivs:\t1', 'Seccomp:\t2', 'CapEff:\t0000000000000000',
                     'CapBnd:\t0000000000000000', 'CapAmb:\t0000000000000000']:
        assert expected in status, expected
    root_mount = next(line.split() for line in Path('/proc/mounts').read_text().splitlines()
                      if line.split()[1] == '/')
    assert 'ro' in root_mount[3].split(','), 'root filesystem must be mounted read-only'
    assert not Path('/var/run/secrets/kubernetes.io/serviceaccount/token').exists()
    assert not any(os.environ.get(key) for key in (
        'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN',
        'AWS_WEB_IDENTITY_TOKEN_FILE', 'GITHUB_TOKEN', 'GH_TOKEN',
    ))
    assert importlib.metadata.version('codegraphcontext') == '0.6.13'
    import codegraphcontext
    source = Path(codegraphcontext.__file__)
    assert source.stat().st_uid == 0
    for protected in [source, Path('/usr/local/bin/cgc'), Path('/etc/passwd'), Path('/forbidden')]:
        refused_write(protected)
    assert '0.6.13' in command('cgc', '--version')
    if '--missing-data' in sys.argv:
        output = command('cgc', 'config', 'set', 'DEFAULT_DATABASE', 'kuzudb', success=False)
        assert 'Read-only file system' in output or 'Permission denied' in output, output
        print(json.dumps({'missing_data_volume': 'denied', 'system_writes': 'denied'}))
        return
    (Path('/tmp') / 'writable').write_text('scratch')
    output = command('cgc', 'config', 'set', 'DEFAULT_DATABASE', 'kuzudb')
    assert '/data/.codegraphcontext/.env' in output
    config = Path('/data/.codegraphcontext/.env')
    assert 'DEFAULT_DATABASE=kuzudb' in config.read_text()
    # Exercise the real indexer and reopen its persistent graph in separate CLI
    # processes. Markdown has no parser-download prerequisite in an offline gate.
    repository = Path('/data/repo')
    repository.mkdir()
    (repository / 'README.md').write_text('Disposable CodeGraph storage fixture\n')
    command('cgc', 'index', str(repository))
    output = command('cgc', 'query', 'MATCH (n:Repository) RETURN n.path AS path')
    assert '"path": "/data/repo"' in output, output
    output = command('cgc', 'query', 'MATCH (n:File) RETURN n.path AS path')
    assert '"path": "/data/repo/README.md"' in output, output
    # The read-only query surface must still refuse mutations.
    output = command('cgc', 'query', 'MATCH (n:Repository) DELETE n', success=False)
    assert 'only supports read-only queries' in output, output
    output = command('cgc', 'query', 'MATCH (n:Repository) RETURN n.path AS path')
    assert '"path": "/data/repo"' in output, output
    output = command('cgc', 'delete', str(repository), '--yes', success=False)
    assert 'Repository deletion is disabled' in output, output
    # Enable deletion only inside this disposable fixture, then verify the real
    # delete path. Production defaults remain denied.
    command('cgc', 'config', 'set', 'ALLOW_DB_DELETION', 'true')
    command('cgc', 'delete', str(repository), '--yes')
    output = command('cgc', 'query', 'MATCH (n:Repository) RETURN count(n) AS remaining')
    assert '"remaining": 0' in output, output
    assert any(p.is_file() for p in Path('/data/.codegraphcontext').rglob('*') if p != config)
    print(json.dumps({'config': 'persisted', 'database': 'create/reopen/read/delete passed',
                      'system_writes': 'denied', 'uid': os.getuid()}))


if __name__ == '__main__':
    main()
