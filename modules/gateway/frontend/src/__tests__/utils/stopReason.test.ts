/**
 * Tests for the stop_reason → prose mapping (issue #4187).
 *
 * A run stopped by a spend cap is not a failure, and the operator reading the
 * board needs to be told which cap fired and who can raise it. The worker sends
 * a static enum; this module owns the wording, so the wording can change without
 * rebuilding the agent image.
 *
 * As with `skipReason.ts`, the edges are what matter: the worker and the SPA
 * deploy independently, so an enum this build has never seen must still render
 * something an operator can act on.
 */
import { describe, it, expect } from 'vitest';
import { describeStopReason, isBudgetStoppedStatus } from '@/utils/stopReason';

describe('describeStopReason', () => {
  it('distinguishes the per-run cap from the per-chain cap', () => {
    // These are different remedies: one run overspent vs. a fan-out overspent in
    // aggregate. Collapsing them into one sentence would hide which is which.
    expect(describeStopReason('run_cap_exceeded')).toMatch(/per-run spend cap/i);
    expect(describeStopReason('chain_cap_exceeded')).toMatch(/per-chain spend cap/i);
  });

  it('names the administrator for a hierarchy cap', () => {
    // The user cannot fix an exhausted org budget themselves, so the prose has to
    // point at who can — otherwise the message is a dead end.
    expect(describeStopReason('hierarchy_cap_exceeded')).toMatch(/administrator/i);
  });

  it('returns null for null / undefined / empty so callers can use it as a guard', () => {
    expect(describeStopReason(null)).toBeNull();
    expect(describeStopReason(undefined)).toBeNull();
    expect(describeStopReason('')).toBeNull();
  });

  it('humanizes an unmapped reason instead of hiding it', () => {
    expect(describeStopReason('some_future_cap')).toBe('Some future cap');
  });

  it('falls back to the raw value when a reason is only separators', () => {
    expect(describeStopReason('___')).toBe('___');
  });

  it('does not treat the reason as markup', () => {
    const out = describeStopReason('<script>alert(1)</script>');
    expect(out).not.toBeNull();
    expect(typeof out).toBe('string');
  });
});

describe('isBudgetStoppedStatus', () => {
  it('is true only for budget_stopped', () => {
    expect(isBudgetStoppedStatus('budget_stopped')).toBe(true);
  });

  it('is false for failed', () => {
    // The whole point of the separate status: a cap firing is the control
    // working, and reporting it as a failure sends operators to debug a run that
    // behaved correctly.
    expect(isBudgetStoppedStatus('failed')).toBe(false);
  });

  it('is false for other statuses and for null / undefined', () => {
    expect(isBudgetStoppedStatus('complete')).toBe(false);
    expect(isBudgetStoppedStatus('no_op')).toBe(false);
    expect(isBudgetStoppedStatus(null)).toBe(false);
    expect(isBudgetStoppedStatus(undefined)).toBe(false);
  });
});
