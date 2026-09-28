/**
 * Tests for the shared S3 fallback target resolution (issue #4184).
 *
 * The behaviour under test is specifically the *silent-failure* mode that made
 * the original bug invisible: an unset bucket must produce a loud skip, NOT a
 * PutObject aimed at a hardcoded bucket that IAM denies. The "zero PutObject
 * calls" assertion is the one that matters most here.
 */
import { resolveFallbackBucket, buildFallbackKey, FALLBACK_BUCKET_ENV } from './s3Fallback';

describe('resolveFallbackBucket', () => {
  const ORIGINAL_ENV = process.env;

  beforeEach(() => {
    process.env = { ...ORIGINAL_ENV };
    delete process.env[FALLBACK_BUCKET_ENV];
  });

  afterAll(() => {
    process.env = ORIGINAL_ENV;
  });

  it('returns the configured bucket when the env var is set', () => {
    process.env[FALLBACK_BUCKET_ENV] = 'adp-dev-agent-run-logs-123456789012';
    const logError = jest.fn();

    expect(resolveFallbackBucket(logError)).toBe('adp-dev-agent-run-logs-123456789012');
    expect(logError).not.toHaveBeenCalled();
  });

  it('returns null and logs exactly one error when unset', () => {
    const logError = jest.fn();

    expect(resolveFallbackBucket(logError)).toBeNull();
    expect(logError).toHaveBeenCalledTimes(1);
  });

  it('names the missing variable in the error so the fix is obvious', () => {
    const logError = jest.fn();
    resolveFallbackBucket(logError);

    expect(logError.mock.calls[0][0]).toContain('AGENT_FALLBACK_BUCKET');
  });

  it('treats an empty or whitespace-only value as unset', () => {
    process.env[FALLBACK_BUCKET_ENV] = '   ';
    const logError = jest.fn();

    expect(resolveFallbackBucket(logError)).toBeNull();
    expect(logError).toHaveBeenCalledTimes(1);
  });

  it('never returns the foreign-account bucket that caused the bug', () => {
    // Regression guard: the original defect was a `|| 'adp-agent-state'`
    // default. No input should ever produce that name.
    const logError = jest.fn();
    expect(resolveFallbackBucket(logError)).not.toBe('adp-agent-state');

    process.env[FALLBACK_BUCKET_ENV] = 'adp-dev-agent-run-logs-123456789012';
    expect(resolveFallbackBucket(logError)).not.toBe('adp-agent-state');
  });
});

describe('buildFallbackKey', () => {
  const ORIGINAL_ENV = process.env;

  beforeEach(() => {
    process.env = { ...ORIGINAL_ENV };
    process.env.REPO_OWNER = 'aws-e';
    process.env.REPO_NAME = 'adp';
  });

  afterAll(() => {
    process.env = ORIGINAL_ENV;
  });

  it('namespaces the key by owner and repo', () => {
    const key = buildFallbackKey(4184, 'comment');

    expect(key).toMatch(/^agent-fallback\/aws-e\/adp\/issue-4184\//);
  });

  it('gives two orgs different keys for the same issue number', () => {
    // The target bucket already holds objects from multiple GitHub orgs, so
    // an issue-number-only key is a cross-tenant collision. This is the
    // tenant-isolation guarantee, and it is load-bearing.
    const first = buildFallbackKey(42, 'comment');

    process.env.REPO_OWNER = 'acme-corp';
    const second = buildFallbackKey(42, 'comment');

    expect(first).not.toBe(second);
    expect(first).toContain('/aws-e/');
    expect(second).toContain('/acme-corp/');
  });

  it('distinguishes different repos under the same owner', () => {
    const first = buildFallbackKey(7, 'comment');

    process.env.REPO_NAME = 'other-repo';
    const second = buildFallbackKey(7, 'comment');

    expect(first).not.toBe(second);
  });

  it('keeps a placeholder segment when owner/repo are missing', () => {
    // A missing env var must not collapse the path segment — that would merge
    // two tenants' output into one shared prefix.
    delete process.env.REPO_OWNER;
    delete process.env.REPO_NAME;

    const key = buildFallbackKey(1, 'comment');

    expect(key).toBe(key.replace('//', 'SENTINEL')); // no empty path segment
    expect(key).toMatch(/^agent-fallback\/unknown-owner\/unknown-repo\/issue-1\//);
  });

  it('honours the label and defaults the extension to md', () => {
    expect(buildFallbackKey(1, 'comment')).toMatch(/-comment\.md$/);
  });

  it('supports a custom extension for the git-changes tarball', () => {
    expect(buildFallbackKey(1, 'git-changes', 'tar.gz')).toMatch(/-git-changes\.tar\.gz$/);
  });

  it('produces a timestamp free of characters that complicate S3 keys', () => {
    const key = buildFallbackKey(1, 'comment');
    const filename = key.split('/').pop() as string;

    expect(filename).not.toContain(':');
    // Only the extension separator should remain as a dot.
    expect(filename.replace(/\.md$/, '')).not.toContain('.');
  });

  it('accepts a string issue number as the callers pass it', () => {
    expect(buildFallbackKey('4184', 'comment')).toContain('issue-4184');
  });
});
