import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { validate } from '@forge/manifest';

test('Atlassian validates the built admin page and scheduled remote manifest', async () => {
  const root = process.cwd();
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'adp-forge-manifest-'));
  try {
    const manifest = fs.existsSync('manifest.yml') ? fs.readFileSync('manifest.yml', 'utf8') :
      fs.readFileSync('manifest.template.yml', 'utf8')
        .replace('__FORGE_APP_ID__', 'ari:cloud:ecosystem::app/11111111-1111-1111-1111-111111111111')
        .replace('__ADP_BASE_URL__', 'https://example.com');
    fs.writeFileSync(path.join(temporary, 'manifest.yml'), manifest);
    fs.symlinkSync(path.join(root, 'static'), path.join(temporary, 'static'));
    process.chdir(temporary);
    const result = await validate();
    assert.equal(result.success, true, JSON.stringify(result.errors));
  } finally {
    process.chdir(root);
    fs.rmSync(temporary, { recursive: true, force: true });
  }
});
