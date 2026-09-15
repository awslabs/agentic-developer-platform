/**
 * Tests for the shared invocation-status renderer (issue #4400).
 *
 * These exist because the extraction they cover is a *consolidation*: three
 * near-identical `STATUS_CONFIG` maps became one. The risk of that change is not
 * a crash, it is a silent behaviour change — a label quietly dropped, or an
 * unmapped status suddenly reading as something it isn't. So the tests below pin
 * the things a consolidation eats:
 *
 * 1. The `webhook_received` label really does differ between the table and the
 *    modal, and both forms survive. The modal's longer form had **no test at
 *    all** before this file, which is precisely how it would have been lost.
 * 2. Every other status is identical in both variants, so the variant parameter
 *    cannot silently change a label it was not meant to touch.
 * 3. An unknown status degrades visibly rather than being mislabelled.
 */

import { describe, it, expect } from 'vitest';
import { describeStatus, statusLabel, KNOWN_STATUSES } from '@/utils/status';
import type { InvocationStatus } from '@/types/activity';

describe('describeStatus', () => {
  it('renders every known status with a glyph, a label and a colour', () => {
    // Guards against a status being added to the union but not to the map, which
    // would fall through to the no_op fallback and render a real run as "✗ No-op".
    for (const status of KNOWN_STATUSES) {
      const presentation = describeStatus(status);
      expect(presentation.glyph, `${status} has no glyph`).toBeTruthy();
      expect(presentation.label, `${status} has no label`).toBeTruthy();
      expect(presentation.colorClass, `${status} has no colour`).toContain('text-');
    }
  });

  it('keeps the compact and full labels for webhook_received distinct', () => {
    // The table column cannot fit the full word; the modal always showed it. This
    // is the one divergence the three-copy consolidation had to preserve, and the
    // assertion that would have caught it being flattened.
    expect(describeStatus('webhook_received', 'compact').label).toBe('Webhook recv');
    expect(describeStatus('webhook_received', 'full').label).toBe('Webhook received');
  });

  it('defaults to the compact label', () => {
    // The table and cards are the majority of call sites, so the safe default is
    // the one that fits a narrow column.
    expect(describeStatus('webhook_received').label).toBe('Webhook recv');
  });

  it('uses one label for every status that does not need a short form', () => {
    // If the variant parameter ever started changing other labels, the modal and
    // the table would disagree about what happened to a run.
    for (const status of KNOWN_STATUSES.filter((s) => s !== 'webhook_received')) {
      expect(describeStatus(status, 'compact').label, `${status} diverged between variants`).toBe(
        describeStatus(status, 'full').label,
      );
    }
  });

  it('styles blocked and skipped neutrally, not as errors', () => {
    // Issue #4020: a guard that stopped a spawn and a worker that deduplicated a
    // redelivery are both correct behaviour. Red sends operators to investigate
    // nothing.
    for (const status of ['blocked', 'skipped'] as InvocationStatus[]) {
      expect(describeStatus(status).colorClass, `${status} is styled as an error`).not.toContain('red');
      expect(describeStatus(status).colorClass).toBe(describeStatus('no_op').colorClass);
    }
  });

  it('styles budget_stopped amber rather than red', () => {
    // Issue #4187: the cap worked as configured. This reads as "needs a budget
    // decision", not "something is broken" — and with the drill-down (#4400) it
    // is the status most likely to be clicked through from.
    expect(describeStatus('budget_stopped').colorClass).toContain('amber');
    expect(describeStatus('budget_stopped').colorClass).not.toContain('red');
  });

  it('styles aborted amber rather than red', () => {
    // Issue #3964: a person stopped this run on purpose. Red would report an
    // operator's own intervention back to them as a fault, which is how you get
    // someone debugging a run that behaved exactly as asked.
    expect(describeStatus('aborted').colorClass).toContain('amber');
    expect(describeStatus('aborted').colorClass).not.toContain('red');
  });

  it('gives aborted a glyph distinct from budget_stopped', () => {
    // Issue #3964: both are amber, so colour alone cannot separate "stopped by
    // hand" from "refused by a cap" — and those are different answers to the
    // question an operator is actually asking, "why did this end?". If the glyphs
    // ever collapse, the two become indistinguishable on the board.
    expect(describeStatus('aborted').glyph).not.toBe(describeStatus('budget_stopped').glyph);
    expect(describeStatus('aborted').label).toBe('Aborted');
  });

  it('exposes aborted as a known status', () => {
    // KNOWN_STATUSES drives the exhaustiveness loop above and is exported for
    // filters. A status missing from it renders through the `?? no_op` fallback —
    // a deliberately stopped run shown as "✗ No-op".
    expect(KNOWN_STATUSES).toContain('aborted');
  });

  it('does not render a provider native interruption as an aborted run', () => {
    // Issue #3964, harness-neutral contract. Only a confirmed ADP abort
    // finalization carries `aborted`; adapters normalize their own outcomes before
    // anything is written, so these strings should never reach the frontend — and
    // if one does, it must degrade visibly rather than borrow the aborted badge.
    // `AbortError` is the pointed case: substring-matching on "abort" is exactly
    // the shortcut this forbids.
    for (const native of ['interrupted', 'cancelled', 'AbortError', 'ECONNRESET']) {
      const presentation = describeStatus(native);
      expect(presentation.label, `${native} was mapped to a real status`).toBe(native);
      expect(presentation.colorClass).toBe(describeStatus('no_op').colorClass);
      expect(presentation.glyph).not.toBe(describeStatus('aborted').glyph);
    }
  });

  it('degrades an unrecognised status to neutral styling with the raw value', () => {
    // The API may add a status on its own cadence and this SPA ships on another.
    // The unknown value must be visible and inert — never relabelled as a status
    // it is not, and never a crash.
    const presentation = describeStatus('some_future_status');
    expect(presentation.label).toBe('some_future_status');
    expect(presentation.colorClass).toBe(describeStatus('no_op').colorClass);
  });

  it('renders a missing status as Unknown rather than guessing', () => {
    for (const value of [null, undefined, '']) {
      expect(describeStatus(value).label).toBe('Unknown');
    }
  });
});

describe('statusLabel', () => {
  it('returns just the label, honouring the variant', () => {
    expect(statusLabel('complete')).toBe('Complete');
    expect(statusLabel('webhook_received', 'full')).toBe('Webhook received');
  });

  it('agrees with describeStatus for every status', () => {
    // One function delegating to the other, asserted so they cannot fork.
    for (const status of KNOWN_STATUSES) {
      expect(statusLabel(status)).toBe(describeStatus(status).label);
    }
  });
});
