/**
 * Unit tests for the RFC-6902 JSON Patch applier used by STATE_DELTA (#4208).
 *
 * The old inline handling treated the whole pointer as one top-level key, so
 * `/draft/intent` wrote a key literally named "draft/intent" and the UI never
 * saw it. These tests pin down nested resolution, RFC-6901 unescaping, and the
 * "never throw on a malformed event" contract.
 */

import { describe, it, expect } from 'vitest';
import {
  applyPatch,
  applyPatches,
  parsePointer,
  unescapePointerToken,
} from '@/utils/jsonPatch';

describe('unescapePointerToken', () => {
  it('decodes ~1 to / and ~0 to ~', () => {
    expect(unescapePointerToken('a~1b')).toBe('a/b');
    expect(unescapePointerToken('a~0b')).toBe('a~b');
  });

  it('decodes ~01 as ~1 rather than as an escaped slash', () => {
    // RFC-6901 requires ~1 first then ~0; getting the order wrong turns the
    // literal token "~1" into "/".
    expect(unescapePointerToken('a~01b')).toBe('a~1b');
  });
});

describe('parsePointer', () => {
  it('treats the empty pointer and "/" as the document root', () => {
    expect(parsePointer('')).toEqual([]);
    expect(parsePointer('/')).toEqual([]);
  });

  it('splits into decoded reference tokens', () => {
    expect(parsePointer('/draft/open~1questions/0')).toEqual([
      'draft',
      'open/questions',
      '0',
    ]);
  });
});

describe('applyPatch', () => {
  it('replaces a top-level key (the shape update_draft emits)', () => {
    const out = applyPatch({ a: 1 }, { op: 'replace', path: '/a', value: 2 });
    expect(out).toEqual({ a: 2 });
  });

  it('resolves a NESTED pointer instead of writing a literal slashed key', () => {
    const out = applyPatch(
      { draft: { intent: 'old' } } as Record<string, unknown>,
      { op: 'replace', path: '/draft/intent', value: 'new' },
    );
    expect(out).toEqual({ draft: { intent: 'new' } });
    expect(out['draft/intent']).toBeUndefined();
  });

  it('vivifies a missing intermediate object for a nested add', () => {
    const out = applyPatch({}, { op: 'add', path: '/draft/intent', value: 'x' });
    expect(out).toEqual({ draft: { intent: 'x' } });
  });

  it('vivifies an array when the next token is an index', () => {
    const out = applyPatch({}, { op: 'add', path: '/draft/outcomes/0', value: 'x' });
    expect(out).toEqual({ draft: { outcomes: ['x'] } });
  });

  it('appends with the "-" array token', () => {
    const out = applyPatch(
      { list: ['a'] } as Record<string, unknown>,
      { op: 'add', path: '/list/-', value: 'b' },
    );
    expect(out).toEqual({ list: ['a', 'b'] });
  });

  it('inserts at an index on add and overwrites on replace', () => {
    expect(
      applyPatch({ l: ['a', 'c'] } as Record<string, unknown>, {
        op: 'add',
        path: '/l/1',
        value: 'b',
      }),
    ).toEqual({ l: ['a', 'b', 'c'] });

    expect(
      applyPatch({ l: ['a', 'c'] } as Record<string, unknown>, {
        op: 'replace',
        path: '/l/1',
        value: 'b',
      }),
    ).toEqual({ l: ['a', 'b'] });
  });

  it('removes an object key and splices an array element', () => {
    expect(applyPatch({ a: 1, b: 2 }, { op: 'remove', path: '/a' })).toEqual({ b: 2 });
    expect(
      applyPatch({ l: ['a', 'b', 'c'] } as Record<string, unknown>, {
        op: 'remove',
        path: '/l/1',
      }),
    ).toEqual({ l: ['a', 'c'] });
  });

  it('honours escaped tokens when writing', () => {
    const out = applyPatch({}, { op: 'add', path: '/a~1b', value: 1 });
    expect(out).toEqual({ 'a/b': 1 });
  });

  it('does not mutate the input (React needs new references)', () => {
    const nested = { intent: 'old' };
    const input = { draft: nested } as Record<string, unknown>;

    const out = applyPatch(input, { op: 'replace', path: '/draft/intent', value: 'new' });

    expect(nested.intent).toBe('old');
    expect(out).not.toBe(input);
    expect(out.draft).not.toBe(nested);
  });

  // -------------------------------------------------------------------------
  // Malformed events must not take down the chat UI
  // -------------------------------------------------------------------------

  it('returns the input unchanged for an unknown op', () => {
    const input = { a: 1 };
    expect(applyPatch(input, { op: 'move', path: '/a', value: 2 })).toBe(input);
  });

  it('returns the input unchanged for a root-level pointer', () => {
    const input = { a: 1 };
    expect(applyPatch(input, { op: 'replace', path: '', value: 2 })).toBe(input);
  });

  it('returns the input unchanged when removing along a path that does not exist', () => {
    const input = { a: 1 };
    expect(applyPatch(input, { op: 'remove', path: '/nope/deeper' })).toBe(input);
  });

  it('returns the input unchanged for a non-index array token', () => {
    const input = { l: ['a'] } as Record<string, unknown>;
    expect(applyPatch(input, { op: 'replace', path: '/l/oops', value: 'b' })).toBe(input);
  });
});

describe('applyPatches', () => {
  it('applies ops in order, so a later op wins', () => {
    const out = applyPatches({} as Record<string, unknown>, [
      { op: 'add', path: '/draft', value: { intent: 'first' } },
      { op: 'replace', path: '/draft/intent', value: 'second' },
    ]);
    expect(out).toEqual({ draft: { intent: 'second' } });
  });

  it('keeps applying valid ops after a malformed one', () => {
    const out = applyPatches({} as Record<string, unknown>, [
      { op: 'copy', path: '/x', value: 1 },
      { op: 'add', path: '/draft', value: { intent: 'ok' } },
    ]);
    expect(out).toEqual({ draft: { intent: 'ok' } });
  });

  it('is a no-op for an empty op list', () => {
    const input = { a: 1 };
    expect(applyPatches(input, [])).toBe(input);
  });
});
