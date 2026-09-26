"""Actual offline Python/TypeScript/Go parser manifest acceptance."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, '/app')
from parser_manifest import ParseInputManifest, compute_tree_digest  # noqa: E402
from scip_proto.scip_pb2 import Index  # noqa: E402


def main():
    if not __debug__:
        raise RuntimeError('Acceptance requires assertions')
    assert os.getuid() == os.getgid() == 10001
    for package in ('boto3', 'botocore', 'httpx', 'requests', 'psycopg2', 'gremlinpython'):
        assert importlib.util.find_spec(package) is None, package
    for key in ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN',
                'AWS_WEB_IDENTITY_TOKEN_FILE', 'GH_TOKEN', 'GITHUB_TOKEN'):
        assert not os.environ.get(key), key
    assert not Path('/var/run/secrets/kubernetes.io/serviceaccount/token').exists()
    for path in ('/app/isolated_parser.py', '/etc/passwd', '/source/main.py'):
        try:
            with open(path, 'ab'):
                pass
        except PermissionError:
            pass
        except OSError as exc:
            assert exc.errno == 30
        else:
            raise AssertionError('protected path writable: ' + path)
    digest = compute_tree_digest('/source')
    output = Path('/output')
    manifest = ParseInputManifest(invocation_id='fixture-invocation', asset_id='fixture/repo',
                                 attempt_id='fixture-attempt', source_dir='/source',
                                 source_digest=digest, allowed_languages=['python', 'typescript', 'go'],
                                 output_dir=str(output))
    input_path = Path('/tmp/input-manifest.json')
    input_path.write_text(manifest.to_json())
    environment = {**os.environ, 'PARSER_INPUT_MANIFEST': str(input_path),
                   'GOPROXY': 'off', 'GOSUMDB': 'off'}
    result = subprocess.run([sys.executable, '/app/isolated_parser.py'], env=environment,
                            capture_output=True, text=True, timeout=600)
    assert result.returncode == 0, (result.stdout, result.stderr)
    payload = json.loads((output / 'output_manifest.json').read_text())
    assert payload['status'] == 'complete', payload
    assert payload['invocation_id'] == manifest.invocation_id
    assert payload['asset_id'] == manifest.asset_id
    assert payload['attempt_id'] == manifest.attempt_id
    languages = {row['language']: row for row in payload['languages']}
    for language, filename in [('python', 'main.py'), ('typescript', 'main.ts'), ('go', 'main.go')]:
        row = languages[language]
        assert row['success'], (row, result.stderr)
        wire = (output / row['scip_path']).read_bytes()
        assert hashlib.sha256(wire).hexdigest() == row['digest']
        assert len(wire) == row['output_bytes']
        index = Index()
        index.ParseFromString(wire)
        assert any(d.relative_path.endswith(filename) and d.occurrences for d in index.documents), language
    assert compute_tree_digest('/source') == digest
    invalid = manifest.to_dict()
    invalid['source_digest'] = '0' * 64
    invalid['output_dir'] = '/output/rejected'
    input_path.write_text(json.dumps(invalid))
    rejected = subprocess.run([sys.executable, '/app/isolated_parser.py'], env=environment,
                              capture_output=True, text=True, timeout=15)
    assert rejected.returncode == 1
    failure = json.loads((output / 'rejected/output_manifest.json').read_text())
    assert failure['status'] == 'error' and failure['error'] == 'source digest mismatch'
    assert not list((output / 'rejected').glob('*.scip'))
    print(json.dumps({'uid': os.getuid(), 'indexers': sorted(languages),
                      'manifest_digests': 'verified', 'source_digest_mismatch': 'rejected',
                      'source_unchanged': True, 'credentials': 'absent'}))


if __name__ == '__main__':
    main()
