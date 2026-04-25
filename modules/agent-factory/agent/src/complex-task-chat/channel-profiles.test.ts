import { getChannelDirective, getChannelEffort } from './channel-profiles';

describe('getChannelDirective', () => {
  it('returns non-empty string for webchat with character limit mention', () => {
    const directive = getChannelDirective('webchat');
    expect(directive).toBeTruthy();
    expect(directive.length).toBeGreaterThan(0);
    expect(directive).toMatch(/4000/); // mentions character limit
    expect(directive).toMatch(/publish_artifact/); // mentions artifact publishing
  });

  it('webchat directive instructs the agent to try tools before declining', () => {
    // Regression guard: a YouTube-summary ask was refused with a static
    // "I can't watch videos" reply even though WebFetch was available and
    // would have returned the page HTML. The directive must tell the agent
    // to attempt tools on any concrete resource (URL, path, hash, command)
    // before concluding it can't help.
    const d = getChannelDirective('webchat');
    expect(d).toMatch(/Try your tools before declining/i);
    expect(d).toMatch(/WebFetch/);
    expect(d).toMatch(/YouTube/); // explicit call-out of the worst offender
    expect(d).toMatch(/knowledge cutoff/i); // forbids the "my knowledge has a cutoff" refusal
    expect(d).toMatch(/can't watch/i); // forbids "I can't watch video"
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
