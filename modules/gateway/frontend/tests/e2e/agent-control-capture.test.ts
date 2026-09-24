import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync, rmSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { verifyServedAssets, verifyForLiveAcceptance, finalizeCapture, SENTINEL_POD_TOKEN, validateInputs } from './agent-control-capture.ts';

test('one matching asset cannot hide an unrecognized executable bundle', () => {
  const dir = mkdtempSync(join(tmpdir(), 'control-assets-'));
  try {
    const manifest = join(dir, 'manifest.json');
    writeFileSync(manifest, JSON.stringify({revision: 'rev', assets: {'/assets/style.css': 'sha256:css'}}));
    assert.equal(verifyServedAssets('rev', {'/assets/style.css': 'sha256:css'}, manifest).verified, true);
    const result = verifyServedAssets('rev', {'/assets/style.css': 'sha256:css', '/assets/other.js': 'sha256:unknown'}, manifest);
    assert.equal(result.verified, false);
    assert.deepEqual(result.mismatched_assets, ['/assets/other.js']);
  } finally { rmSync(dir, {recursive: true, force: true}); }
});

test('live mode and a verified-asset flag cannot make an incomplete capture admissible', () => {
  assert.throws(() => verifyForLiveAcceptance({mode: 'live', served_assets: {verified: true, method: 'asset_manifest_match'}}), /scenario/);
});

test('incomplete diagnostics cannot archive a credential sentinel', () => {
  const dir = mkdtempSync(join(tmpdir(), 'control-private-'));
  try {
    const meta = { mode: 'mocked', gateway_url: SENTINEL_POD_TOKEN, bundle_revision: 'rev',
      served_assets: {verified: false, method: 'local_build_hash', claimed_revision: 'rev', observed_assets: {}, matched_assets: [], mismatched_assets: []},
      run_id: 'run', abort_run_id: 'other', observed_generations: [], injected_conditions: [], captured_at: '', spec_digest: '', scenarios_executed: [] };
    assert.throws(() => finalizeCapture(dir, meta as Parameters<typeof finalizeCapture>[1]), /credential sentinel/);
    assert.equal(existsSync(join(dir, 'browser_control_run.partial.json')), false);
    assert.equal(existsSync(join(dir, 'browser_control_run.json')), false);
  } finally { rmSync(dir, {recursive: true, force: true}); }
});


test('live setup refuses expired second session before any scenario can run', () => {
  const dir = mkdtempSync(join(tmpdir(), 'control-inputs-'));
  try {
    const manifest = join(dir, 'manifest.json');
    const owner = join(dir, 'owner.json');
    const nonowner = join(dir, 'nonowner.json');
    writeFileSync(manifest, JSON.stringify({revision: 'rev', assets: {'/assets/app.js': 'sha256:js'}}));
    writeFileSync(owner, JSON.stringify({access_token: 'access', id_token: 'id', expires_at_ms: Date.now() + 60000}));
    writeFileSync(nonowner, JSON.stringify({access_token: 'access-other', id_token: 'id-other', expires_at_ms: 1}));
    const env = {CONTROL_E2E_LIVE: '1', CONTROL_E2E_CAPTURE_DIR: join(dir, 'capture'),
      GATEWAY_URL: 'https://fixture.example', CONTROL_E2E_DISABLED_URL: 'https://disabled.example',
      CONTROL_E2E_BUNDLE_REVISION: 'rev', CONTROL_E2E_ASSET_MANIFEST: manifest,
      CONTROL_E2E_RUN_ID: 'one', CONTROL_E2E_ABORT_RUN_ID: 'two',
      CONTROL_E2E_SESSION_FILE: owner, CONTROL_E2E_NONOWNER_SESSION_FILE: nonowner};
    assert.throws(() => validateInputs(env), /NONOWNER.*expired/);
    assert.throws(() => validateInputs({...env, CONTROL_E2E_ABORT_RUN_ID: 'one'}), /distinct runs/);
  } finally { rmSync(dir, {recursive: true, force: true}); }
});
