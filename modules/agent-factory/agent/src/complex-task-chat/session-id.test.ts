/**
 * Tests for session id shape validation (#5660 / A07).
 *
 * These assert REFUSAL. The ids that must keep working are asserted too, because
 * an over-strict rule here locks existing users out of their own conversations.
 */
import {
  assertValidSessionId,
  isValidSessionId,
  InvalidSessionIdError,
  MAX_SESSION_ID_LENGTH,
  RESERVED_SESSION_IDS,
} from './session-id';

describe('session id shape validation', () => {
  describe('the ids real clients actually mint stay valid', () => {
    // If these regress, existing conversations become unresumable — the
    // migration risk called out in the issue's impact analysis.
    it.each([
      ['SPA format (AgentChat.tsx)', 'sess-1758441600000-a1b2c3d'],
      ['CLI uuid5 format (intake_dispatch.py)', 'sess-3f2a1b9c8d7e6f5a4b3c2d1e0f9a8b7c'],
      ['plain uuid', '550e8400-e29b-41d4-a716-446655440000'],
      ['underscores', 'sess_local_dev_1'],
      ['single character', 'a'],
      // These two are live formats, not hypotheticals. Rejecting either would
      // break existing conversations — a worse regression than the bug fixed
      // here — so they are pinned as acceptance criteria.
      ['Slack thread_ts (channels/slack.py)', '1758441600.123456'],
      ['session_key fallback (channels/base.py)', 'webchat:C0123ABC:user-alice'],
      ['slack session_key fallback', 'slack:C0123ABC:U04ABCDEF:1758441600.123456'],
    ])('accepts %s', (_label, id) => {
      expect(isValidSessionId(id)).toBe(true);
      expect(assertValidSessionId(id)).toBe(id);
    });

    it('accepts an id at exactly the length limit', () => {
      const id = 'a'.repeat(MAX_SESSION_ID_LENGTH);
      expect(isValidSessionId(id)).toBe(true);
    });
  });

  describe('a path separator or traversal is refused', () => {
    // Each of these would let a derived storage prefix escape its own session.
    it.each([
      ['forward slash', 'sess-a/sess-b'],
      ['leading slash', '/sess-a'],
      ['trailing slash', 'sess-a/'],
      ['parent traversal', '../other-tenant'],
      ['embedded traversal', 'sess-a/../sess-b'],
      ['bare parent', '..'],
      ['bare current', '.'],
      ['backslash', 'sess-a\\sess-b'],
      ['whitespace', 'sess a'],
      ['NUL byte', 'sess-a\0'],
      ['newline', 'sess-a\n'],
      ['wildcard', 'sess-*'],
      // `.` is in the charset for Slack's thread_ts, so traversal must be
      // refused explicitly rather than falling out of the charset.
      ['traversal with no slash', 'sess..a'],
      ['double dot suffix', 'sess-a..'],
    ])('refuses %s', (_label, id) => {
      expect(isValidSessionId(id)).toBe(false);
      expect(() => assertValidSessionId(id)).toThrow(InvalidSessionIdError);
    });

    it('accepts a single dot inside an id, which cannot traverse', () => {
      // Slack's `1758441600.123456` is the reason the charset admits `.`; a
      // single dot is an ordinary S3 key character, not a separator.
      expect(isValidSessionId('sess.a')).toBe(true);
    });
  });

  describe('an id colliding with the artifact layout root is refused', () => {
    // This is the mass-delete case: the sweeper's legacy prefix was
    // `${sessionId}/`, so `o` yielded `o/` — every tenant's uploads.
    it.each(RESERVED_SESSION_IDS.map(id => [id]))('refuses reserved segment %s', id => {
      expect(isValidSessionId(id)).toBe(false);
      expect(() => assertValidSessionId(id)).toThrow(/reserved artifact key segment/);
    });

    it('still accepts an id that merely starts with a reserved letter', () => {
      // `o` is refused; `org-scoped-session` is a perfectly ordinary id.
      expect(isValidSessionId('o-real-session')).toBe(true);
      expect(isValidSessionId('s3-notes')).toBe(true);
    });
  });

  describe('empty, oversized and non-string values are refused', () => {
    it.each([
      ['empty string', ''],
      ['undefined', undefined],
      ['null', null],
      ['number', 12345],
      ['object', { sessionId: 'x' }],
      ['array', ['sess-a']],
    ])('refuses %s', (_label, id) => {
      expect(isValidSessionId(id)).toBe(false);
      expect(() => assertValidSessionId(id)).toThrow(InvalidSessionIdError);
    });

    it('refuses an id one character over the limit', () => {
      const id = 'a'.repeat(MAX_SESSION_ID_LENGTH + 1);
      expect(isValidSessionId(id)).toBe(false);
      expect(() => assertValidSessionId(id)).toThrow(/exceeds/);
    });
  });

  it('carries the offending id and a reason for the audit log', () => {
    try {
      assertValidSessionId('../escape');
      throw new Error('expected a refusal');
    } catch (err) {
      expect(err).toBeInstanceOf(InvalidSessionIdError);
      expect((err as InvalidSessionIdError).sessionId).toBe('../escape');
      expect((err as InvalidSessionIdError).reason).toBeTruthy();
    }
  });
});
