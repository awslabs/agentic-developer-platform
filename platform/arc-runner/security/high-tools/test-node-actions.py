"""Run real Node20 action bundles and runner hashFiles under the Node24 compatibility path.

Usage inside candidate: python3 test-node-actions.py CHECKOUT_DIR GITHUB_SCRIPT_DIR
The fixtures use local HTTP/git only; no GitHub token or remote repository writes.
"""
import functools
import hashlib
import http.server
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading

NODE = '/home/runner/externals/node20/bin/node'


def run(args, **kwargs):
    return subprocess.run(args, check=True, text=True, capture_output=True, **kwargs)


assert os.environ.get('FORCE_JAVASCRIPT_ACTIONS_TO_NODE24') == 'true'
assert run(['npm', '--version']).stdout.strip() == '11.20.0'
assert Path('/home/runner/externals/node20').resolve() == Path('/home/runner/externals/node24')

with tempfile.TemporaryDirectory(prefix='adp-node24-actions-') as tmp:
    root = Path(tmp)
    source = root / 'source'
    source.mkdir()
    for args in [('init', '-b', 'main'), ('config', 'user.email', 'fixture@example.invalid'),
                 ('config', 'user.name', 'Compatibility Fixture')]:
        run(['git', *args], cwd=source)
    payload = b'Node24 checkout and hashFiles compatibility\n'
    (source / 'marker.txt').write_bytes(payload)
    run(['git', 'add', '.'], cwd=source)
    run(['git', 'commit', '-m', 'Local fixture'], cwd=source)
    expected_commit = run(['git', 'rev-parse', 'HEAD'], cwd=source).stdout.strip()
    remote = root / 'http' / 'fixture' / 'repo'
    remote.parent.mkdir(parents=True)
    run(['git', 'clone', '--bare', str(source), str(remote)])
    run(['git', 'update-server-info'], cwd=remote)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root / 'http'))
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f'http://127.0.0.1:{server.server_port}'
    workspace = root / 'workspace'
    workspace.mkdir()
    output = root / 'output'
    output.touch()
    state = root / 'state'
    state.touch()
    event = root / 'event.json'
    event.write_text('{}')
    env = dict(os.environ, GITHUB_WORKSPACE=str(workspace), GITHUB_REPOSITORY='fixture/repo',
               GITHUB_SERVER_URL=url, GITHUB_API_URL=url, GITHUB_GRAPHQL_URL=url,
               GITHUB_EVENT_PATH=str(event), GITHUB_OUTPUT=str(output), GITHUB_STATE=str(state),
               RUNNER_TEMP=str(root / 'temp'), INPUT_REPOSITORY='fixture/repo', INPUT_REF='main',
               INPUT_TOKEN='local-fixture-token', INPUT_CLEAN='true')
    env.update({'INPUT_PERSIST-CREDENTIALS': 'false', 'INPUT_FETCH-DEPTH': '1',
                'INPUT_SET-SAFE-DIRECTORY': 'true'})
    # Dumb HTTP does not support shallow fetch; exercise a full checkout.
    env['INPUT_FETCH-DEPTH'] = '0'
    checkout = Path(sys.argv[1])
    github_script = Path(sys.argv[2])
    assert 'node20' in (checkout / 'action.yml').read_text()
    assert 'node20' in (github_script / 'action.yml').read_text()
    result = run([NODE, str(checkout / 'dist/index.js')], env=env, cwd=workspace)
    assert (workspace / 'marker.txt').read_bytes() == payload
    assert run(['git', 'rev-parse', 'HEAD'], cwd=workspace).stdout.strip() == expected_commit
    assert 'local-fixture-token' not in (workspace / '.git/config').read_text()
    script_env = dict(env)
    script_env.update({'INPUT_GITHUB-TOKEN': 'local-fixture-token', 'INPUT_DEBUG': 'false',
                      'INPUT_RESULT-ENCODING': 'json', 'INPUT_RETRIES': '0',
                      'INPUT_SCRIPT': '''
const assert = require('assert');
assert.equal(process.versions.node.split('.')[0], '24');
const result = await exec.getExecOutput('git', ['rev-parse', 'HEAD']);
assert.match(result.stdout.trim(), /^[0-9a-f]{40}$/);
core.setOutput('runtime', process.versions.node);
return {passed: true, node: process.versions.node};
'''})
    run([NODE, str(github_script / 'dist/index.js')], env=script_env, cwd=workspace)
    assert '"passed":true' in output.read_text()
    hash_env = dict(env, patterns='marker.txt')
    hashed = run([NODE, '/home/runner/bin/hashFiles'], env=hash_env, cwd=workspace)
    expected_hash = hashlib.sha256(hashlib.sha256(payload).digest()).hexdigest()
    assert f'__OUTPUT__{expected_hash}__OUTPUT__' in hashed.stderr, hashed.stderr
    # The CLI must support package installation and package executable dispatch.
    package = root / 'package'
    package.mkdir()
    (package / 'package.json').write_text(json.dumps({'name': 'adp-local-fixture', 'version': '1.0.0',
                                                    'bin': {'adp-fixture': 'cli.js'}}))
    (package / 'cli.js').write_text('#!/usr/bin/env node\nconsole.log("fixture-ok")\n')
    run(['npm', 'install', '--offline', '--ignore-scripts', '--no-audit', '--no-fund', str(package)], cwd=workspace)
    assert run(['npm', 'exec', '--offline', '--', 'adp-fixture'], cwd=workspace).stdout.strip() == 'fixture-ok'
    print(json.dumps({'node': run([NODE, '--version']).stdout.strip(), 'checkout': expected_commit,
                      'github_script': 'passed', 'runner_hash_files': expected_hash,
                      'npm_install_exec': 'passed'}))
    server.shutdown()
