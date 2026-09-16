/**
 * Tests for organization identifier derivation — Issue #4841 (#4839 · T2a).
 *
 * The identifier is required by the canonical org-create route and immutable after
 * create, so a bad derivation is permanent. These cover the shapes an admin actually
 * types into the name field.
 */

import { describe, it, expect } from 'vitest';
import {
  deriveOrgIdentifier,
  isValidOrgIdentifier,
  ORG_IDENTIFIER_MAX_LENGTH,
} from '@/utils/orgIdentifier';

describe('deriveOrgIdentifier', () => {
  it('lowercases and hyphenates a plain multi-word name', () => {
    expect(deriveOrgIdentifier('Acme Corp')).toBe('acme-corp');
  });

  it('leaves an already-slug-shaped name unchanged', () => {
    expect(deriveOrgIdentifier('sophos')).toBe('sophos');
  });

  it('collapses runs of punctuation and whitespace into a single hyphen', () => {
    expect(deriveOrgIdentifier('Foo   &&  Bar,  Inc.')).toBe('foo-bar-inc');
  });

  it('trims leading and trailing separators rather than emitting bare hyphens', () => {
    expect(deriveOrgIdentifier('  --Acme--  ')).toBe('acme');
  });

  it('strips accents to an ASCII form instead of dropping the character', () => {
    // Dropping it would turn "Björn" into "bjrn"; the slug must stay recognisable.
    expect(deriveOrgIdentifier('Björn Industries')).toBe('bjorn-industries');
    expect(deriveOrgIdentifier('Café Group')).toBe('cafe-group');
  });

  it('preserves digits', () => {
    expect(deriveOrgIdentifier('Studio 54')).toBe('studio-54');
  });

  it('returns empty string when the name has no derivable characters', () => {
    // The caller renders this as "type an identifier" rather than POSTing an empty id
    // the server would reject with a bare 422.
    expect(deriveOrgIdentifier('...')).toBe('');
    expect(deriveOrgIdentifier('   ')).toBe('');
    expect(deriveOrgIdentifier('株式会社')).toBe('');
  });

  it('truncates to the server max length without leaving a trailing hyphen', () => {
    const name = `${'a'.repeat(ORG_IDENTIFIER_MAX_LENGTH - 1)} b`;
    const derived = deriveOrgIdentifier(name);

    expect(derived.length).toBeLessThanOrEqual(ORG_IDENTIFIER_MAX_LENGTH);
    expect(derived.endsWith('-')).toBe(false);
  });
});

describe('isValidOrgIdentifier', () => {
  it('accepts a normal slug', () => {
    expect(isValidOrgIdentifier('acme-corp')).toBe(true);
  });

  it('rejects empty and whitespace-only values', () => {
    expect(isValidOrgIdentifier('')).toBe(false);
    expect(isValidOrgIdentifier('   ')).toBe(false);
  });

  it('rejects a value longer than the server accepts', () => {
    expect(isValidOrgIdentifier('a'.repeat(ORG_IDENTIFIER_MAX_LENGTH + 1))).toBe(false);
  });

  it('accepts a hand-typed value the derivation would not produce', () => {
    // Deliberate: this panel must not invent id semantics the deferred C9 pass owns. The
    // server stores what it is given, so refusing this client-side would be a second,
    // undocumented schema.
    expect(isValidOrgIdentifier('Acme_Corp')).toBe(true);
  });
});
