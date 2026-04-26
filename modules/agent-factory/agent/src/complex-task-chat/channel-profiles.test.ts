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

  it('webchat directive instructs the agent to emit a plan on the first turn', () => {
    // Before: the agent went silent during multi-tool research turns,
    // then dumped one big reply. Users saw tool chips but no indication
    // of what the agent was working on. Fix: tell the model to emit a
    // plan as the first turn's output before any tool calls.
    const d = getChannelDirective('webchat');
    expect(d).toMatch(/Planning protocol/i);
    // The first turn must be plan-only, no tool calls.
    expect(d).toMatch(/FIRST turn must contain ONLY a short plan/);
    // Concrete output shape — ends with "Starting now."
    expect(d).toContain('Starting now.');
    // Skip clause — trivial asks must NOT get a plan.
    expect(d).toMatch(/Skip planning when the task is trivial/i);
  });

  it('webchat directive keeps mid-execution narration guidance', () => {
    // The plan isn't enough on its own — during execution the agent
    // should still emit a sentence before pivots or slow steps. Keeps
    // the user informed during the gap between "Starting now" and the
    // final answer.
    const d = getChannelDirective('webchat');
    expect(d).toMatch(/Narrate mid-execution/i);
    expect(d).toMatch(/one sentence per decision point/i);
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
