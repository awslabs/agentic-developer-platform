/**
 * Tests for the skip_reason → prose mapping (issue #4020).
 *
 * The board used to render a bare "✗ No-op" badge with no explanation, so an
 * operator asking "why didn't my review run?" had to read CloudWatch. The
 * reason now arrives as a static enum and this module turns it into a sentence.
 *
 * The interesting cases are the edges, not the happy path: an enum the producer
 * added after this build shipped must still render something useful, since the
 * Lambda and the SPA deploy independently.
 */
import { describe, it, expect } from 'vitest';
import {
  describeSkipReason,
  skipReasonLabel,
  isNonRunStatus,
  NON_RUN_STATUSES,
} from '@/utils/skipReason';

describe('describeSkipReason', () => {
  it('maps a known intent-parser reason to prose', () => {
    expect(describeSkipReason('no_mention')).toBe('No agent was mentioned in this comment.');
  });

  it('maps a spawn_persona block_reason to prose', () => {
    // These strings are reused verbatim from the Lambda's guards rather than
    // re-invented, so the mapping has to cover both vocabularies.
    expect(describeSkipReason('self_re_trigger')).toMatch(/infinite loop/i);
    expect(describeSkipReason('chain_depth_exceeded')).toMatch(/depth limit/i);
  });

  it('maps the worker-side dedup reason to prose', () => {
    expect(describeSkipReason('idempotency_merged_pr')).toMatch(/already exists/i);
  });

  it('returns null for null / undefined / empty so callers can use it as a guard', () => {
    expect(describeSkipReason(null)).toBeNull();
    expect(describeSkipReason(undefined)).toBeNull();
    expect(describeSkipReason('')).toBeNull();
  });

  it('humanizes an unmapped reason instead of hiding it', () => {
    // Producers ship on a different cadence than the SPA, so an unknown enum is
    // normal. Degrading to a blank cell would reintroduce the exact complaint
    // this issue was filed about.
    expect(describeSkipReason('some_future_reason')).toBe('Some future reason');
  });

  it('handles hyphenated and multi-underscore unknown reasons', () => {
    expect(describeSkipReason('weird--reason__here')).toBe('Weird reason here');
  });

  it('falls back to the raw value when a reason is only separators', () => {
    // Pathological, but humanizing "___" to "" would render as an empty cell and
    // read as a UI bug rather than as odd data.
    expect(describeSkipReason('___')).toBe('___');
  });

  it('does not treat the reason as markup', () => {
    // The enum contract forbids payload echoes, but this layer must not rely on
    // the producer having held that line. Output is plain text, returned as-is
    // for React to escape on render.
    const out = describeSkipReason('<script>alert(1)</script>');
    expect(out).not.toBeNull();
    expect(typeof out).toBe('string');
  });
});

describe('skipReasonLabel', () => {
  it('strips the trailing period for inline use', () => {
    expect(skipReasonLabel('no_mention')).toBe('No agent was mentioned in this comment');
  });

  it('returns null when there is no reason', () => {
    expect(skipReasonLabel(null)).toBeNull();
  });

  it('leaves a period-less label untouched', () => {
    expect(skipReasonLabel('some_future_reason')).toBe('Some future reason');
  });
});

describe('isNonRunStatus', () => {
  it('is true for the three statuses that mean "nothing ran"', () => {
    expect(isNonRunStatus('no_op')).toBe(true);
    expect(isNonRunStatus('blocked')).toBe(true);
    expect(isNonRunStatus('skipped')).toBe(true);
  });

  it('is false for statuses that represent a real run', () => {
    // A reason must never render next to "Complete" — that would describe why
    // nothing ran on a row where something did.
    expect(isNonRunStatus('complete')).toBe(false);
    expect(isNonRunStatus('in_progress')).toBe(false);
    expect(isNonRunStatus('failed')).toBe(false);
    expect(isNonRunStatus('rejected')).toBe(false);
    expect(isNonRunStatus('rate_limited')).toBe(false);
  });

  it('is false for webhook_received', () => {
    // Non-triggering on the board, but it is not a terminal non-run — the row is
    // mid-flight, so there is no reason to show.
    expect(isNonRunStatus('webhook_received')).toBe(false);
  });

  it('is false for null / undefined', () => {
    expect(isNonRunStatus(null)).toBe(false);
    expect(isNonRunStatus(undefined)).toBe(false);
  });
});

describe('NON_RUN_STATUSES', () => {
  it('matches the backend list minus webhook_received', () => {
    // The backend's NON_TRIGGERING_STATUSES is this set plus webhook_received;
    // AgentActivity composes the two so they cannot drift.
    expect([...NON_RUN_STATUSES].sort()).toEqual(['blocked', 'no_op', 'skipped']);
  });
});
