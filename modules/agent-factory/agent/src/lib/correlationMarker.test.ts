/**
 * Unit tests for lib/correlationMarker.ts
 */

import { prependCorrelationMarker } from './correlationMarker';

describe('prependCorrelationMarker', () => {
  const originalEnv = process.env;

  beforeEach(() => {
    process.env = { ...originalEnv };
  });

  afterEach(() => {
    process.env = originalEnv;
  });

  it('prepends marker when env vars are set', () => {
    process.env.ADP_CORRELATION_ID = 'corr-123';
    process.env.ADP_ROOT_HUMAN_ID = 'user-456';
    process.env.ADP_IS_HUMAN_ROOTED = 'true';

    const result = prependCorrelationMarker('Hello world');
    expect(result).toMatch(/^<!-- adp-correlation:corr-123/);
    expect(result).toContain('adp-root-human:user-456');
    expect(result).toContain('adp-is-human-rooted:true');
    expect(result.endsWith('\nHello world')).toBe(true);
  });

  it('returns body unchanged when ADP_CORRELATION_ID is missing', () => {
    delete process.env.ADP_CORRELATION_ID;
    process.env.ADP_ROOT_HUMAN_ID = 'user-456';

    const result = prependCorrelationMarker('Hello world');
    expect(result).toBe('Hello world');
  });

  it('returns body unchanged when ADP_ROOT_HUMAN_ID is missing', () => {
    process.env.ADP_CORRELATION_ID = 'corr-123';
    delete process.env.ADP_ROOT_HUMAN_ID;

    const result = prependCorrelationMarker('Hello world');
    expect(result).toBe('Hello world');
  });

  it('is idempotent - does not double-prepend', () => {
    process.env.ADP_CORRELATION_ID = 'corr-123';
    process.env.ADP_ROOT_HUMAN_ID = 'user-456';
    process.env.ADP_IS_HUMAN_ROOTED = 'true';

    const first = prependCorrelationMarker('Hello world');
    const second = prependCorrelationMarker(first);
    expect(first).toBe(second);
  });

  it('detects existing marker from a different correlation', () => {
    process.env.ADP_CORRELATION_ID = 'new-corr';
    process.env.ADP_ROOT_HUMAN_ID = 'new-user';

    const body = '<!-- adp-correlation:old-corr adp-root-human:old-user adp-is-human-rooted:true -->\nContent';
    const result = prependCorrelationMarker(body);
    expect(result).toBe(body); // Should NOT add a new marker
  });

  it('defaults ADP_IS_HUMAN_ROOTED to false', () => {
    process.env.ADP_CORRELATION_ID = 'corr-123';
    process.env.ADP_ROOT_HUMAN_ID = 'user-456';
    delete process.env.ADP_IS_HUMAN_ROOTED;

    const result = prependCorrelationMarker('Hello');
    expect(result).toContain('adp-is-human-rooted:false');
  });

  it('handles empty body', () => {
    process.env.ADP_CORRELATION_ID = 'corr-123';
    process.env.ADP_ROOT_HUMAN_ID = 'user-456';
    process.env.ADP_IS_HUMAN_ROOTED = 'true';

    const result = prependCorrelationMarker('');
    expect(result).toMatch(/^<!-- adp-correlation:corr-123/);
    expect(result.endsWith('-->\n')).toBe(true);
  });

  // --- Issue #2149: adp-dispatch marker ---

  it('includes adp-dispatch when dispatch_persona is set', () => {
    process.env.ADP_CORRELATION_ID = 'corr-123';
    process.env.ADP_ROOT_HUMAN_ID = 'user-456';
    process.env.ADP_IS_HUMAN_ROOTED = 'true';

    const result = prependCorrelationMarker('@agent-developer please implement', 'developer');
    expect(result).toContain('adp-dispatch:developer');
    expect(result).toContain('adp-correlation:corr-123');
    expect(result).toContain('adp-root-human:user-456');
  });

  it('does NOT include adp-dispatch when dispatch_persona is undefined', () => {
    process.env.ADP_CORRELATION_ID = 'corr-123';
    process.env.ADP_ROOT_HUMAN_ID = 'user-456';
    process.env.ADP_IS_HUMAN_ROOTED = 'true';

    const result = prependCorrelationMarker('## @agent-developer Started');
    expect(result).not.toContain('adp-dispatch');
  });

  it('does NOT include adp-dispatch when dispatch_persona is empty string', () => {
    process.env.ADP_CORRELATION_ID = 'corr-123';
    process.env.ADP_ROOT_HUMAN_ID = 'user-456';
    process.env.ADP_IS_HUMAN_ROOTED = 'true';

    const result = prependCorrelationMarker('body', '');
    expect(result).not.toContain('adp-dispatch');
  });

  it('includes adp-invocation and adp-chain-depth when env vars set', () => {
    process.env.ADP_CORRELATION_ID = 'corr-123';
    process.env.ADP_ROOT_HUMAN_ID = 'user-456';
    process.env.ADP_IS_HUMAN_ROOTED = 'true';
    process.env.ADP_MESSAGE_ID = 'msg-789';
    process.env.ADP_CHAIN_DEPTH = '2';

    const result = prependCorrelationMarker('Body');
    expect(result).toContain('adp-invocation:msg-789');
    expect(result).toContain('adp-chain-depth:2');
  });

  it('full marker with dispatch_persona includes all fields in single line', () => {
    process.env.ADP_CORRELATION_ID = 'corr-full';
    process.env.ADP_ROOT_HUMAN_ID = 'user-full';
    process.env.ADP_IS_HUMAN_ROOTED = 'true';
    process.env.ADP_MESSAGE_ID = 'msg-full';
    process.env.ADP_CHAIN_DEPTH = '3';

    const result = prependCorrelationMarker('@agent-reviewer please review', 'reviewer');
    const lines = result.split('\n');
    // First line is the marker
    expect(lines[0]).toMatch(/^<!--.*-->$/);
    expect(lines[0]).toContain('adp-correlation:corr-full');
    expect(lines[0]).toContain('adp-root-human:user-full');
    expect(lines[0]).toContain('adp-is-human-rooted:true');
    expect(lines[0]).toContain('adp-invocation:msg-full');
    expect(lines[0]).toContain('adp-chain-depth:3');
    expect(lines[0]).toContain('adp-dispatch:reviewer');
    // Body follows on next line
    expect(lines[1]).toBe('@agent-reviewer please review');
  });
});
