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
 *
 * Issue #4687 added the third thing a label here has to carry: **scope**. Both entity
 * types above are scoped to the organization they were authored in, but only one of
 * them reads that way — "User — cloud agents" looked like a cap on the person, so
 * admins authored it expecting to bound somebody's total agent spend and got a cap on
 * whatever fraction of it happens to bill to this workspace. The cross-workspace
 * control is a different row entirely (`person_budget_configs`, keyed by the
 * cross-org person anchor rather than by a per-tenant `users.id`), so the two cannot
 * be reconciled by wording alone: the workspace-scoped label has to SAY it is
 * workspace-scoped, and it has to point at the thing that isn't.
 */

import { EntityType } from '@/types';
import { WORKSPACE_TERM, WORKSPACE_TERM_PLURAL } from '@/utils/budgetVocabulary';

/**
 * The user-facing noun for a tenant/organization partition.
 *
 * One constant because the distinction these labels draw is only legible if every
 * surface draws it with the same word: "this workspace" against "all workspaces" is a
 * scope contrast, while "this org" against "all workspaces" reads as two unrelated
 * facts. Issue #4685 (the two-tile `/budget` page) needs the same noun for the same
 * reason. #4685's `budgetVocabulary.ts` merged first and carries the operator's
 * amended term ('GitHub org' — users know GitHub orgs; nobody knows what a
 * workspace is), so this is a RE-EXPORT, not a second definition (review fix on
 * #4688): two constants for one noun is the drift both files exist to prevent.
 */
export const WORKSPACE_NOUN = WORKSPACE_TERM;

/** Friendly label per entity type, keyed by the wire value. */
const ENTITY_TYPE_LABELS: Record<string, string> = {
  [EntityType.ORGANIZATION]: 'Organization',
  [EntityType.DEPARTMENT]: 'Department',
  [EntityType.TEAM]: 'Team',
  [EntityType.USER]: 'User — direct use',
  // Issue #4687: the parenthetical is the whole point. Without it this label is the
  // only cap-authoring option that names a *person*, so it gets read as a cap on that
  // person — and a cap on the person is `PERSON_LIMIT_LABEL` below, not this.
  [EntityType.ROOT_USER]: `User — cloud agents (this ${WORKSPACE_NOUN})`,
};

/**
 * The label for the cross-workspace person limit — Issue #4687.
 *
 * Deliberately NOT an entry in `ENTITY_TYPE_LABELS`: a person limit is not an entity
 * type and never crosses the wire as one. It is a row in `person_budget_configs`
 * keyed by the person anchor, written through `PUT /budget/person-cap/{anchor}`, and
 * putting it in the entity-type map is how it would end up submitted as
 * `entity_type: 'person_limit'` — a budget config the server has no such type for.
 */
export const PERSON_LIMIT_LABEL = `Person limit — all ${WORKSPACE_TERM_PLURAL}`;

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
 * The synthetic picker value for the cross-workspace person limit — Issue #4687.
 *
 * Prefixed so it can never collide with an `EntityType`, and asserted against the
 * real enum by a test: this value selects a different API (the person-cap PUT) rather
 * than a different `entity_type`, so a collision would route a person limit into the
 * budget-config create path and write a cap under a key enforcement never reads.
 */
export const PERSON_LIMIT_OPTION_VALUE = '__person_limit__';

/**
 * The help text distinguishing the person-scoped options.
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
      // Issue #4687 kept the bucket ("agent runs", the #4536 distinction from direct
      // use) and added the scope, then the redirect. An admin who wanted "cap this
      // person" needs to be told, at the moment they are about to author the wrong
      // thing, that the right thing exists — after the fact they find out from spend
      // that never stopped.
      return `Spend by agent runs this person triggers, counting only the runs that bill to this ${WORKSPACE_NOUN}. For a limit that follows the person everywhere, use ${PERSON_LIMIT_LABEL}.`;
    case PERSON_LIMIT_OPTION_VALUE:
      return `This person's total agent spend across every ${WORKSPACE_NOUN} they work in, not just this one.`;
    default:
      return undefined;
  }
}
