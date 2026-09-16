/**
 * Tests for the shared entity-label map — Issue #4536.
 *
 * The requirement these pin is a hard one from the issue: **nowhere in the UI does
 * the string `root_user` appear.** Every budget surface renders through this module,
 * so the guarantee is testable in one place rather than screen by screen.
 */

import { describe, it, expect } from 'vitest';
import { EntityType } from '@/types';
import {
  formatEntityType,
  entityTypeHelpText,
  PERSON_LIMIT_LABEL,
  PERSON_LIMIT_OPTION_VALUE,
  WORKSPACE_NOUN,
} from '@/utils/entityLabels';

describe('formatEntityType', () => {
  it('names the two person-scoped kinds distinctly and in plain language', () => {
    // Distinct because they are separate ledgers with separate caps: capping one
    // leaves the other unbounded, so the labels must say which is which.
    expect(formatEntityType(EntityType.USER)).toBe('User — direct use');
    // Issue #4687 added the scope qualifier. Unqualified, this was the only option
    // naming a person, so it read as a cap on that person rather than on the slice of
    // their agent spend that bills here.
    expect(formatEntityType(EntityType.ROOT_USER)).toBe('User — cloud agents (this GitHub org)');
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

// Issue #4687: the workspace-scoped cap and the cross-workspace person limit are
// different rows written through different endpoints. These pin the two properties that
// keep an admin from authoring one while believing they authored the other.
describe('workspace-scoped cap vs person limit (#4687)', () => {
  it('says on the workspace-scoped cap that it is workspace-scoped', () => {
    // The bug: this label named a person and said nothing about scope, so admins
    // authored it to bound somebody's total agent spend and bounded one workspace's
    // slice of it.
    expect(formatEntityType(EntityType.ROOT_USER)).toContain(`this ${WORKSPACE_NOUN}`);
  });

  it('points from the workspace-scoped cap at the control that is not scoped', () => {
    // Being told at authoring time is the entire fix — after the fact you find out
    // from spend that never stopped.
    expect(entityTypeHelpText(EntityType.ROOT_USER)).toContain(PERSON_LIMIT_LABEL);
  });

  it('keeps naming the bucket the workspace-scoped cap governs', () => {
    // Regression on #4536: adding scope must not cost the direct-use/cloud-agents
    // distinction, which is what tells an admin which of a person's dollars this caps.
    expect(entityTypeHelpText(EntityType.ROOT_USER)).toMatch(/agent runs/i);
  });

  it('names the person limit as covering every workspace', () => {
    expect(PERSON_LIMIT_LABEL).toContain('all GitHub orgs');
    expect(entityTypeHelpText(PERSON_LIMIT_OPTION_VALUE)).toMatch(/every GitHub org/i);
  });

  it('never presents the person limit as an entity type', () => {
    // A person limit is a `person_budget_configs` row keyed by the person anchor, not
    // a budget config. If `formatEntityType` knew this value, the option could be
    // submitted as `entity_type` and write a cap under a key enforcement never reads —
    // the #4511 failure class one ledger over.
    expect(formatEntityType(PERSON_LIMIT_OPTION_VALUE)).toBe(PERSON_LIMIT_OPTION_VALUE);
    expect(Object.values(EntityType) as string[]).not.toContain(PERSON_LIMIT_OPTION_VALUE);
  });

  it('keeps the person limit option value out of the EntityType namespace', () => {
    // Sentinel-shaped on purpose: a collision would route a person limit into the
    // budget-config create path.
    expect(PERSON_LIMIT_OPTION_VALUE).toBe('__person_limit__');
  });
});
