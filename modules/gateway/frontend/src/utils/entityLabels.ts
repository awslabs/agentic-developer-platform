/**
 * Plain-language names for the entities a budget can be set on — Issue #4536.
 *
 * The load-bearing requirement: **no ledger vocabulary reaches the screen.** A person
 * budgeting a team thinks "how much can Priya's agents spend this month?", not in
 * entity types, so `root_user` must never be rendered anywhere in the UI. Keeping the
 * map here rather than inline in one page is what makes that hold: the create dropdown,
 * the list badge, the type filter and the read-only edit view all read the same entry,
 * so a surface cannot fall back to printing the raw wire value.
 *
 * The two person-scoped kinds are a real distinction, not a naming quirk — they are
 * separate ledgers with separate caps, keyed in different id namespaces:
 *
 * - `user` — **direct use**: traffic the person sends while signed in (laptop tools,
 *   the dashboard). Keyed by Cognito sub.
 * - `root_user` — **cloud agents**: spend by agent runs the person triggered. Keyed by
 *   canonical `users.id`.
 *
 * Capping one leaves the other unbounded, which is exactly why the labels must say
 * which is which. The wording mirrors the server's own line labels on `/budget`
 * (`me_routes._PER_PERSON_SOURCES`) so someone who authors a cap on one screen
 * recognises the line it governs on the other.
 */

import { EntityType } from '@/types';

/** Friendly label per entity type, keyed by the wire value. */
const ENTITY_TYPE_LABELS: Record<string, string> = {
  [EntityType.ORGANIZATION]: 'Organization',
  [EntityType.DEPARTMENT]: 'Department',
  [EntityType.TEAM]: 'Team',
  [EntityType.USER]: 'User — direct use',
  [EntityType.ROOT_USER]: 'User — cloud agents',
};

/**
 * Render an entity type for display.
 *
 * Falls back to the raw value for a type this map does not know — an unrenderable
 * label would be worse than an unfamiliar one — but every type the budget API accepts
 * is covered above, so the fallback is unreachable for authored budgets.
 */
export function formatEntityType(type: string): string {
  return ENTITY_TYPE_LABELS[type] || type;
}

/**
 * The help text distinguishing the two person-scoped kinds.
 *
 * Shown next to the entity picker, because the label alone ("direct use" vs "cloud
 * agents") is only meaningful once you know which of your dollars lands where. Empty
 * for the shared entity types, which need no disambiguation.
 */
export function entityTypeHelpText(type: string): string | undefined {
  switch (type) {
    case EntityType.USER:
      return 'Laptop tools, dashboard — traffic this person sends while signed in.';
    case EntityType.ROOT_USER:
      return 'Spend by agent runs this person triggers.';
    default:
      return undefined;
  }
}
