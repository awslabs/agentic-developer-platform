import { getChannelDirective, getChannelEffort } from './channel-profiles';

describe('getChannelDirective', () => {
  it('returns non-empty string for webchat with character limit mention', () => {
    const directive = getChannelDirective('webchat');
    expect(directive).toBeTruthy();
    expect(directive.length).toBeGreaterThan(0);
    expect(directive).toMatch(/4000/); // mentions character limit
    expect(directive).toMatch(/publish_artifact/); // mentions artifact publishing
  });

  it('returns non-empty string for slack', () => {
    const directive = getChannelDirective('slack');
    expect(directive).toBeTruthy();
    expect(directive.length).toBeGreaterThan(0);
    expect(directive).toMatch(/3000/); // mentions Slack truncation limit
    expect(directive).toMatch(/mrkdwn/); // mentions Slack formatting
  });

  it('returns empty string for empty channel', () => {
    expect(getChannelDirective('')).toBe('');
  });

  it('returns empty string for unknown channel', () => {
    expect(getChannelDirective('whatsapp')).toBe('');
    expect(getChannelDirective('sms')).toBe('');
  });

  it('returns empty string for undefined-like inputs', () => {
    expect(getChannelDirective('')).toBe('');
  });
});

describe('getChannelEffort', () => {
  it("returns 'medium' for webchat (concise but preserves structure)", () => {
    expect(getChannelEffort('webchat')).toBe('medium');
  });

  it('returns undefined for slack (use SDK default)', () => {
    expect(getChannelEffort('slack')).toBeUndefined();
  });

  it('returns undefined for empty string', () => {
    expect(getChannelEffort('')).toBeUndefined();
  });

  it('returns undefined for unknown channels', () => {
    expect(getChannelEffort('whatsapp')).toBeUndefined();
  });
});
