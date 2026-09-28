/**
 * The one word for a tenant boundary in budget copy — Issue #4685 (#4669 ruling).
 *
 * The `/budget` surfaces called the same boundary three things: "organization"
 * (`PersonSpendingLimit`), "workspace" (`PerOrgSpend`), and "org" (per-line labels).
 * A reader cannot tell whether "across every organization" and "across every
 * workspace" are the same set, so they cannot tell whether two figures on the page
 * count the same dollars. The ruling (amended 2026-09-05) picked **GitHub org** —
 * the one name users already know, since tenants mirror GitHub orgs 1:1 — and this
 * module is where that choice lives so another synonym cannot arrive by copy edit.
 * "workspace" and "tenant" are internal vocabulary and must not reach a user.
 *
 * **Labels only, never a field name.** The wire says `org_id`, `per_org`, `org`, and
 * `EntityType.ORGANIZATION`; none of that changes, because renaming a display term is
 * a copy decision and renaming a wire field is a contract break. Anything imported
 * from here belongs in a sentence a user reads.
 */

/** The user-facing noun for one tenant boundary. */
export const WORKSPACE_TERM = 'GitHub org';

/** The plural, for uncounted phrases ("across all workspaces"). */
export const WORKSPACE_TERM_PLURAL = 'GitHub orgs';
