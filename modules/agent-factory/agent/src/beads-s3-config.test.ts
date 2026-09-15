/**
 * Beads S3 bucket/region configuration (issue #4184, defect 2).
 *
 * Two defects are covered:
 *  - a hardcoded `us-west-2` region against us-east-1 infrastructure, and
 *  - a hardcoded `adp-agent-state` bucket that lives in a foreign AWS account
 *    and is permitted by no statement in the agent's IAM policy.
 *
 * The issue named beads.ts:37, but that line is overwritten by configureBeads()
 * at both call sites before any bd command runs — the LIVE defect was in
 * agent-pm.ts. Both are asserted here so the fix cannot regress at either
 * reference, which is the "enumerate every hardcoded region reference on this
 * path" requirement rather than just the one line the issue named.
 */
import * as fs from 'fs';
import * as path from 'path';
import { configureBeads, getBeadsConfig } from './beads';

const readSrc = (...p: string[]) => fs.readFileSync(path.join(__dirname, ...p), 'utf-8');

describe('beads S3 configuration defaults', () => {
  const ORIGINAL_ENV = process.env;

  afterEach(() => {
    process.env = ORIGINAL_ENV;
  });

  it('defaults to no bucket rather than a bucket IAM denies', () => {
    // Empty is the safe default: syncPull/syncPush guard on `!config.s3Bucket`
    // and skip with a warning, so an unconfigured remote degrades to a clean
    // no-op instead of a denied write.
    jest.resetModules();
    const fresh = require('./beads');

    expect(fresh.getBeadsConfig().s3Bucket).toBe('');
    expect(fresh.getBeadsConfig().s3Bucket).not.toBe('adp-agent-state');
  });

  it('sources the default region from AWS_REGION', () => {
    process.env = { ...ORIGINAL_ENV, AWS_REGION: 'eu-west-1' };
    jest.resetModules();
    const fresh = require('./beads');

    expect(fresh.getBeadsConfig().s3Region).toBe('eu-west-1');
  });

  it('falls back to the deployed region, not us-west-2, when AWS_REGION is unset', () => {
    process.env = { ...ORIGINAL_ENV };
    delete process.env.AWS_REGION;
    jest.resetModules();
    const fresh = require('./beads');

    expect(fresh.getBeadsConfig().s3Region).toBe('us-east-1');
  });

  it('still lets callers override via configureBeads', () => {
    configureBeads({ s3Bucket: 'adp-dev-agent-beads-state-123456789012', s3Region: 'us-east-2' });

    expect(getBeadsConfig().s3Bucket).toBe('adp-dev-agent-beads-state-123456789012');
    expect(getBeadsConfig().s3Region).toBe('us-east-2');
  });
});

describe('no hardcoded us-west-2 or foreign bucket remains on this path', () => {
  it('beads.ts carries neither literal in code', () => {
    const source = readSrc('beads.ts');

    expect(source).not.toMatch(/s3Region:\s*'us-west-2'/);
    expect(source).not.toMatch(/s3Bucket:\s*'adp-agent-state'/);
  });

  it('agent-pm.ts sources region from env and drops the foreign bucket default', () => {
    // This was the live defect — the one the issue's literal instruction missed.
    const source = readSrc('agent-pm.ts');

    expect(source).not.toMatch(/BEADS_S3_REGION\s*=\s*process\.env\.BEADS_S3_REGION\s*\|\|\s*'us-west-2'/);
    expect(source).not.toMatch(/BEADS_S3_BUCKET\s*=\s*process\.env\.BEADS_S3_BUCKET\s*\|\|\s*'adp-agent-state'/);
    expect(source).toContain('process.env.AWS_REGION');
  });

  it('agent-worker.ts drops the foreign bucket default for beads too', () => {
    // The hosted-path beads bucket carried the same literal.
    const source = readSrc('agent-worker.ts');

    expect(source).not.toMatch(/BEADS_S3_BUCKET\s*=\s*process\.env\.BEADS_S3_BUCKET\s*\|\|\s*'adp-agent-state'/);
  });

  it('leaves no adp-agent-state fallback in any S3 write path', () => {
    // The original bug reached six sites from one copy-pasted expression.
    for (const file of [
      ['agent-worker.ts'],
      ['agent-pm.ts'],
      ['beads.ts'],
      ['utils', 'ghPost.ts'],
      ['services', 'S3Fallback.ts'],
    ]) {
      const source = readSrc(...file);
      expect(source).not.toMatch(/\|\|\s*'adp-agent-state'/);
    }
  });

  it('routes every fallback write through the shared resolver', () => {
    // Leaving copies behind is how one bug became six.
    for (const file of [
      ['agent-worker.ts'],
      ['utils', 'ghPost.ts'],
      ['services', 'S3Fallback.ts'],
    ]) {
      expect(readSrc(...file)).toContain('resolveFallbackBucket');
    }
  });
});


test('beads sync restores platform identity while a customer deployment is active', async () => {
  const childProcess = require('child_process');
  const command = jest.spyOn(childProcess, 'execSync').mockReturnValue('pushed');
  const saved = { ...process.env };
  try {
    process.env.ADP_AGENT_AUTHORITY_ENABLED = 'true';
    process.env.ADP_WORKER_IRSA_ROLE_ARN = 'platform-worker';
    process.env.ADP_WORKER_IRSA_TOKEN_FILE = '/projected/worker';
    process.env.ADP_WORKER_AWS_REGION = 'us-east-1';
    process.env.AWS_ACCESS_KEY_ID = 'customer-key';
    process.env.AWS_PROFILE = 'customer';
    process.env.AWS_CONFIG_FILE = '/task/config';
    const beads = require('./beads');
    beads.configureBeads({ s3Bucket: 'platform-beads', s3Region: 'us-east-1' });
    await beads.syncPush('/workspace');
    expect(command).toHaveBeenCalledWith('bd dolt push', expect.objectContaining({
      env: expect.objectContaining({ AWS_ROLE_ARN: 'platform-worker', AWS_WEB_IDENTITY_TOKEN_FILE: '/projected/worker', AWS_CONFIG_FILE: '/dev/null', AWS_REGION: 'us-east-1' }),
    }));
    expect((command.mock.calls[0][1] as { env: NodeJS.ProcessEnv }).env.AWS_ACCESS_KEY_ID).toBeUndefined();
    expect(process.env.AWS_ACCESS_KEY_ID).toBe('customer-key');
  } finally {
    command.mockRestore();
    process.env = saved;
  }
});
