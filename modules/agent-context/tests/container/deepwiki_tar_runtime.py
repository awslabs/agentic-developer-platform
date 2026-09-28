"""Bounded, offline tar controls and the real Next cold/cache SWC consumer."""
import io
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile


def archive(path, name, content, *, pax=None):
    with tarfile.open(path, 'w:gz', format=tarfile.PAX_FORMAT) as output:
        info = tarfile.TarInfo(name)
        info.size = len(content)
        info.pax_headers = pax or {}
        output.addfile(info, io.BytesIO(content))


PROGRAM = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const root = process.env.ADP_TAR_FIXTURE;
const mode = process.env.ADP_TAR_MODE;
const tar = require('/app/node_modules/next/dist/compiled/tar');
(async () => {
  if (mode === 'numeric-pax') {
    const out = path.join(root, 'numeric'); fs.mkdirSync(out);
    await tar.x({file:path.join(root,'numeric.tgz'),cwd:out});
    assert.equal(fs.readFileSync(path.join(out,'12345'),'utf8'),'numeric filename');
    console.log('PASS: numeric PAX filename remains a string');
    return;
  }
  if (mode === 'ratio') {
    const out = path.join(root,'ratio'); fs.mkdirSync(out);
    await assert.rejects(tar.x({file:path.join(root,'ratio.tgz'),cwd:out,
      maxDecompressionRatio:4}), /decompression ratio exceeded/);
    console.log('PASS: bounded decompression ratio rejects the small fixture');
    return;
  }
  assert.equal(require('/app/node_modules/next/dist/compiled/tar/node_modules/tar/package.json').version,'7.5.21');
  assert.equal(tar.__esModule,undefined,'Next requires a plain CommonJS export');
  const bytes = fs.readFileSync(path.join(root,'swc.tgz'));
  let requests = 0;
  const server = http.createServer((req,res) => {
    requests++;
    if(req.url !== '/@next/swc-fixture/-/swc-fixture-fixture.tgz') {
      res.writeHead(404);res.end();return;
    }
    res.writeHead(200,{'content-type':'application/octet-stream'});res.end(bytes);
  });
  await new Promise(resolve => server.listen(0,'127.0.0.1',resolve));
  try {
    process.env.npm_config_registry = `http://127.0.0.1:${server.address().port}/`;
    process.env.npm_config_update_notifier = 'false';
    process.env.NEXT_SWC_PATH = path.join(root,'cache');
    const {getRegistry}=require('/app/node_modules/next/dist/lib/helpers/get-registry');
    assert.equal(getRegistry('/app'),process.env.npm_config_registry);
    const {downloadNativeNextSwc}=require('/app/node_modules/next/dist/lib/download-swc');
    const first=path.join(root,'first'),second=path.join(root,'second');
    await downloadNativeNextSwc('fixture',first,['fixture']);
    assert.equal(fs.readFileSync(path.join(first,'@next/swc-fixture/fixture.node'),'utf8'),'synthetic swc package');
    assert.equal(requests,1);
    await downloadNativeNextSwc('fixture',second,['fixture']);
    assert.equal(fs.readFileSync(path.join(second,'@next/swc-fixture/fixture.node'),'utf8'),'synthetic swc package');
    assert.equal(requests,1,'cache hit must not download again');
    console.log('PASS: actual Next registry helper, cold download, strip:1 extraction and cache reuse');
  } finally { server.closeAllConnections(); await new Promise(resolve=>server.close(resolve)); }
})().catch(error=>{console.error(error);process.exitCode=1;});
'''


def main():
    modes = sys.argv[1:] or ['numeric-pax', 'ratio', 'consumer']
    if any(mode not in ('numeric-pax', 'ratio', 'consumer') for mode in modes):
        raise ValueError('Unknown fixture mode')
    with tempfile.TemporaryDirectory(prefix='adp-next-tar-') as directory:
        root = Path(directory)
        archive(root / 'numeric.tgz', 'placeholder', b'numeric filename', pax={'path': '12345'})
        archive(root / 'ratio.tgz', 'small.txt', b'0' * 8192)
        archive(root / 'swc.tgz', 'package/fixture.node', b'synthetic swc package')
        for mode in modes:
            environment = dict(os.environ, ADP_TAR_FIXTURE=directory, ADP_TAR_MODE=mode)
            subprocess.run(['node', '-e', PROGRAM], env=environment, check=True, timeout=30)


if __name__ == '__main__':
    main()
