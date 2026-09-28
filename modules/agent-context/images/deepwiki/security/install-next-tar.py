"""Replace only the reviewed Next15.5.24 tar bundle with locked runtime packages."""
import hashlib
import json
from pathlib import Path
import shutil
import sys

EXPECTED = '5428466a9c645a4251c3bf892ec1a94f32edd418af222a980b02090f2c9d6d24'

def install(next_root: Path, dependencies: Path):
    if json.loads((next_root / 'package.json').read_text())['version'] != '15.5.24':
        raise RuntimeError('Review the tar adapter for the changed Next version')
    target = next_root / 'dist/compiled/tar'
    if hashlib.sha256((target / 'index.js').read_bytes()).hexdigest() != EXPECTED:
        raise RuntimeError('Unexpected compiled tar bytes; review before replacement')
    if json.loads((dependencies / 'tar/package.json').read_text())['version'] != '7.5.21':
        raise RuntimeError('Unexpected replacement tar version')
    shutil.rmtree(target)
    target.mkdir()
    shutil.copytree(dependencies, target / 'node_modules')
    (target / 'package.json').write_text(json.dumps({
        'name': '@adp/next-tar-adapter', 'version': '1.0.0', 'private': True,
        'main': 'index.cjs', 'description': 'Next tar consumer adapter; actual tar package retained with metadata and license',
    }, indent=2) + '\n')
    (target / 'index.cjs').write_text(
        "'use strict';\n"
        "// Next expects the old plain CommonJS export, not tar7's __esModule marker.\n"
        "const adapter = { ...require('./node_modules/tar') };\n"
        "delete adapter.__esModule;\nmodule.exports = adapter;\n"
    )
    (target / 'adp-source-receipt.json').write_text(json.dumps({
        'next_version': '15.5.24', 'original_bundle_sha256': EXPECTED,
        'replacement': 'tar@7.5.21', 'replacement_lock_sha256': hashlib.sha256(
            (dependencies.parent / 'package-lock.json').read_bytes()).hexdigest(),
    }, indent=2) + '\n')

if __name__ == '__main__':
    install(Path(sys.argv[1]), Path(sys.argv[2]))
