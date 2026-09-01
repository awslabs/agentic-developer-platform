/**
 * Tests for the shared entity-label map — Issue #4536.
 *
 * The requirement these pin is a hard one from the issue: **nowhere in the UI does
 * the string `root_user` appear.** Every budget surface renders through this module,
 * so the guarantee is testable in one place rather than screen by screen.
 */

import { describe, it, expect } from 'vitest';
import { EntityType } from '@/types';
import { formatEntityType, entityTypeHelpText } from '@/utils/entityLabels';

describe('formatEntityType', () => {
  it('names the two person-scoped kinds distinctly and in plain language', () => {
    // Distinct because they are separate ledgers with separate caps: capping one
    // leaves the other unbounded, so the labels must say which is which.
    expect(formatEntityType(EntityType.USER)).toBe('User — direct use');
    expect(formatEntityType(EntityType.ROOT_USER)).toBe('User — cloud agents');
  });

  it('never renders a schema entity value for any type the budget API accepts', () => {
    for (const type of [
      EntityType.ORGANIZATION,
      EntityType.DEPARTMENT,
      EntityType.TEAM,
      EntityType.USER,
      EntityType.ROOT_USER,
    ]) {
      expect(formatEntityType(type)).not.toContain('root_user');
      expect(formatEntityType(type)).not.toContain('_');
    }
  });

  it('leaves the shared entity types worded as before', () => {
    // Regression check: #4536 changed the person-scoped labels only.
    expect(formatEntityType(EntityType.ORGANIZATION)).toBe('Organization');
    expect(formatEntityType(EntityType.DEPARTMENT)).toBe('Department');
    expect(formatEntityType(EntityType.TEAM)).toBe('Team');
  });

  it('falls back to the raw value for an unknown type', () => {
    // An unrenderable label would be worse than an unfamiliar one.
    expect(formatEntityType('something-new')).toBe('something-new');
  });
});

describe('entityTypeHelpText', () => {
  it('distinguishes the two buckets by what the person actually did', () => {
    expect(entityTypeHelpText(EntityType.USER)).toMatch(/signed in/i);
    expect(entityTypeHelpText(EntityType.ROOT_USER)).toMatch(/agent runs/i);
  });

  it('does not describe direct use as agent spend, or vice versa', () => {
    // Swapped help text is the "user caps the wrong bucket" failure mode.
    expect(entityTypeHelpText(EntityType.USER)).not.toMatch(/agent/i);
    expect(entityTypeHelpText(EntityType.ROOT_USER)).not.toMatch(/laptop/i);
  });

  it('is absent for entity types that name a group rather than a person', () => {
    expect(entityTypeHelpText(EntityType.TEAM)).toBeUndefined();
    expect(entityTypeHelpText(EntityType.ORGANIZATION)).toBeUndefined();
  });
});
