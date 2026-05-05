/**
 * Unit tests for LCM Scrubber.
 *
 * Issue #137: Vault Phase 4
 */

import { Scrubber } from './scrubber';

describe('Scrubber', () => {
  let scrubber: Scrubber;

  beforeEach(() => {
    scrubber = new Scrubber();
  });

  it('returns text unchanged when no patterns registered', () => {
    const text = 'hello world with some content';
    expect(scrubber.scrub(text)).toBe(text);
  });

  it('replaces a registered sensitive value', () => {
    scrubber.registerSensitiveValue('ghp_abc123XYZ456', '<<redacted:github:default>>');
    const input = 'Using token ghp_abc123XYZ456 to authenticate';
    expect(scrubber.scrub(input)).toBe('Using token <<redacted:github:default>> to authenticate');
  });

  it('replaces multiple occurrences of the same value', () => {
    scrubber.registerSensitiveValue('my-secret-token', '<<REDACTED>>');
    const input = 'first: my-secret-token, second: my-secret-token';
    expect(scrubber.scrub(input)).toBe('first: <<REDACTED>>, second: <<REDACTED>>');
  });

  it('replaces multiple different registered values', () => {
    scrubber.registerSensitiveValue('github-pat-123456', '<<redacted:github>>');
    scrubber.registerSensitiveValue('jira-api-key-abc', '<<redacted:jira>>');
    const input = 'gh: github-pat-123456, jira: jira-api-key-abc';
    expect(scrubber.scrub(input)).toBe('gh: <<redacted:github>>, jira: <<redacted:jira>>');
  });

  it('ignores values shorter than 8 characters (threshold)', () => {
    scrubber.registerSensitiveValue('short', '<<redacted>>');
    expect(scrubber.scrub('value is short here')).toBe('value is short here');
  });

  it('registers values exactly 8 characters', () => {
    scrubber.registerSensitiveValue('12345678', '<<redacted>>');
    expect(scrubber.scrub('code: 12345678 end')).toBe('code: <<redacted>> end');
  });

  it('ignores empty string registration', () => {
    scrubber.registerSensitiveValue('', '<<redacted>>');
    expect(scrubber.hasPatterns).toBe(false);
    expect(scrubber.scrub('anything')).toBe('anything');
  });

  it('handles values with regex special characters ($, ., +)', () => {
    const token = 'sk-ant$proj.key+test1234';
    scrubber.registerSensitiveValue(token, '<<redacted:api>>');
    expect(scrubber.scrub(`Auth: ${token}`)).toBe('Auth: <<redacted:api>>');
  });

  it('hasPatterns returns true when patterns exist', () => {
    expect(scrubber.hasPatterns).toBe(false);
    scrubber.registerSensitiveValue('long-enough-value', '<<r>>');
    expect(scrubber.hasPatterns).toBe(true);
  });

  it('is task-scoped — new instance has no patterns from previous', () => {
    scrubber.registerSensitiveValue('secret-value-123', '<<old>>');
    const newScrubber = new Scrubber();
    expect(newScrubber.scrub('secret-value-123')).toBe('secret-value-123');
  });

  it('handles multiline text', () => {
    scrubber.registerSensitiveValue('multiline-secret', '<<redacted>>');
    const input = 'line1\ncontains multiline-secret\nline3';
    expect(scrubber.scrub(input)).toBe('line1\ncontains <<redacted>>\nline3');
  });

  it('handles JSON-embedded values', () => {
    const token = 'ghp_aBcDeFgHiJkLmNoPqRsT';
    scrubber.registerSensitiveValue(token, '<<redacted:github:default>>');
    const json = `{"authorization":"Bearer ${token}","data":"test"}`;
    expect(scrubber.scrub(json)).toContain('<<redacted:github:default>>');
    expect(scrubber.scrub(json)).not.toContain(token);
  });
});
