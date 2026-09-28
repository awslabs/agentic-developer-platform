"""Bounded offline Node/npm-tar controls for the installed SCIP toolchain."""
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile


def write_archive(path, files):
    with tarfile.open(path, 'w:gz') as archive:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))


def main():
    if not __debug__:
        raise RuntimeError('Acceptance requires assertions')
    assert os.getuid() == os.getgid() == 10001
    for key in ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN',
                'AWS_WEB_IDENTITY_TOKEN_FILE', 'GH_TOKEN', 'GITHUB_TOKEN'):
        assert not os.environ.get(key), key
    # The same upstream-derived three-case Node regression used for DeepWiki.
    subprocess.run(['node', '/validation/deepwiki_node_runtime.js'], check=True)
    with tempfile.TemporaryDirectory(prefix='scip-npm-fixture-') as directory:
        root = Path(directory)
        write_archive(root / 'ratio.tgz', {'small.txt': b'0' * 8192})
        write_archive(root / 'package.tgz', {
            'package/package.json': json.dumps({'name': 'offline-scip-fixture',
                                               'version': '1.0.0', 'main': 'index.js'}).encode(),
            'package/index.js': b"module.exports = 'fixture';\n",
        })
        program = r'''
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');
const root = process.argv[1];
const tar = require('/usr/lib/node_modules/npm/node_modules/tar');
(async () => {
  const out = path.join(root, 'ratio'); fs.mkdirSync(out);
  await assert.rejects(tar.x({file:path.join(root,'ratio.tgz'),cwd:out,
    maxDecompressionRatio:4}), /decompression ratio exceeded/);
  console.log('PASS: installed npm tar rejects bounded decompression-ratio fixture');
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        subprocess.run(['node', '-e', program, str(root)], check=True, timeout=30)
        subprocess.run(['npm', 'install', '--offline', '--ignore-scripts', '--no-audit',
                        '--no-fund', '--prefix', str(root / 'consumer'),
                        str(root / 'package.tgz')], check=True, timeout=30)
        subprocess.run(['node', '-e', "require('node:assert/strict').equal(require(process.argv[1]), 'fixture')",
                        str(root / 'consumer/node_modules/offline-scip-fixture')], check=True)
    print(json.dumps({'node_nul_hostname': 'rejected', 'npm_tar_ratio': 'rejected',
                      'actual_npm_offline_extract_install': 'passed', 'network': 'disabled'}))


if __name__ == '__main__':
    main()
