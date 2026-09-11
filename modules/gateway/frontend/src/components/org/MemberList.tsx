/**
 * MemberList — an organization's members, their teams, and what they may spend.
 *
 * Issue #4847 (#4839 · T2b). UI contract: `docs/mockups/4839-tenancy-admin.html`
 * (Members tab) plus the v4 operator comment on the issue, which added the
 * SPEND / LIMIT (MONTH) column.
 *
 * ## Purely presentational, exactly like `UserList`
 *
 * This component owns dialog state and nothing else. Every write is a caller
 * callback, and a REJECTED promise deliberately keeps the dialog open so the
 * caller's error banner is read next to the context that produced it — the common
 * failure here is the 409 second-primary refusal, which is only interpretable
 * while you can still see which team you were promoting. Reporting the error is the
 * caller's job; `UserList` states the same contract for the same reason.
 *
 * ## The membership row is the truth; `users.team_id` is a cache of it
 *
 * A person's teams are rendered from membership ROWS (`TeamMembership[]`), one chip
 * each, with the primary marked. Nothing here reads or writes `users.team_id`: the
 * server re-points that column to follow the primary, and a UI that wrote it
 * directly would drift the pointer away from the table that decides membership. So
 * "add a team" is a membership create, and "make primary" is a set-replace — not an
 * edit of a single-valued team field, which is what the old single-team UI had.
 *
 * Multi-team is a DISPLAY and ASSIGNMENT capability only. One budget-bearing team
 * remains — the primary — so the primary marker is not decoration: it names the team
 * whose budget and Bedrock routing actually govern this person. That is why it is
 * rendered as a labelled badge rather than, say, a bold chip.
 *
 * ## The spend column shows the ENFORCED limit, or nothing
 *
 * `budgets` is keyed by `users.id`. A member with no entry renders no figures at
 * all, because this column has two distinct absences that must not collapse into
 * each other:
 *
 * - **no budget row for this member** — the caller could not read the figures (the
 *   batch read is platform-admin-only, a narrower gate than this panel's). The
 *   column is simply absent for that caller.
 * - **`limitUsd === null`** — the server resolved the ladder and NOTHING governs
 *   this person at any rung. Rendered as "no limit", never as `$0.00`: a zero reads
 *   as somebody who may spend nothing, which is the opposite of the truth.
 *
 * Both figures arrive as decimal STRINGS at their own column's precision and are
 * parsed only to size the bar. No spend or limit arithmetic happens here beyond
 * that ratio — the server composes both from the resolvers enforcement uses, so the
 * number displayed is the number enforced. `sourceLabel` is likewise rendered
 * verbatim: only the server knows which rung won, and it composes third-person
 * labels for this surface ("individual limit", "team default", "org default",
 * "platform default") — recomputing the label client-side is how it drifts from
 * the rung actually charged.
 *
 * ## Roles and removal reuse `UserList`'s calls, not new ones (PR #4936 review, M2)
 *
 * The role select fires the caller's `onChangeRole`, which pages wire to the same
 * `assignUserRole` client `UserList` uses — the server (`PUT .../users/{id}`) stays
 * the boundary and refuses self-changes and above-ceiling roles. "Remove" is
 * removal FROM THE ORGANIZATION (`removeOrgUser`, `DELETE .../users/{id}`), a
 * different act from `UserList`'s demote-to-member, so it sits behind the panel's
 * persistent-confirmation convention and the copy says exactly what it deletes.
 */

import { useState } from 'react';
import { Badge, Button, Card, Select, Modal, ModalFooter, Table } from '@/components/ui';
import type { Column } from '@/components/ui/Table';
import type { MemberBudget } from '@/services/admin';
import { AdminRole } from '@/types';
import type { Team, TeamMembership } from '@/types';

/** One row of the panel: a member, their membership rows, and their budget figures. */
export interface OrgMember {
  /**
   * The canonical `users.id`.
   *
   * The value every membership write is keyed by, and what `budgets` is keyed by.
   * Not a Cognito sub and not a GitHub login: those identify a person to a provider,
   * while the membership table joins on this.
   */
  id: string;
  email: string;
  name: string | null;
  /** The linked GitHub login, or `null` for a member who signed up by email. */
  githubUsername: string | null;
  /** Org role (member / dept_admin / org_admin / …), distinct from role-within-team. */
  role: string | null;
}

export interface MemberListProps {
  members: OrgMember[];
  /** Membership rows per `users.id`. A member missing here is on no team. */
  membershipsByUserId: Record<string, TeamMembership[]>;
  /**
   * Every team in the ORG, for the assignment picker and for naming chips.
   *
   * Org-wide on purpose: assigning a second team means choosing from the whole org,
   * and a picker built from a department-scoped list would silently hide every team
   * outside whichever department the admin happened to be viewing.
   */
  teams: Team[];
  /**
   * Month spend + applicable limit per `users.id`. Omit entirely (not `{}` per
   * member) when the caller may not read them — the column disappears rather than
   * rendering rows of blanks that read as "no spend".
   */
  budgets?: Record<string, MemberBudget>;
  /** Add ONE membership row. Rejects with the server's error, which the caller shows. */
  onAddTeam?: (member: OrgMember, teamId: string) => Promise<unknown> | void;
  /**
   * Make `teamId` this member's primary team.
   *
   * Distinct from `onAddTeam` because the server refuses a second primary rather
   * than silently re-pointing the claim: moving a primary is a deliberate act with
   * its own action, not a checkbox on an add form.
   */
  onSetPrimary?: (member: OrgMember, teamId: string) => Promise<unknown> | void;
  /** Remove ONE membership row. */
  onRemoveTeam?: (member: OrgMember, teamId: string) => Promise<unknown> | void;
  /**
   * Change the member's ORG role (member / dept_admin / org_admin / …).
   *
   * Wired by callers to the same `assignUserRole` client `UserList` uses; the
   * server enforces who may assign what. Rendered as a per-row select (the
   * mockup's affordance) rather than `UserList`'s dialog, because here the row
   * already names the person the change applies to.
   */
  onChangeRole?: (member: OrgMember, role: string) => Promise<unknown> | void;
  /**
   * Remove the member from the ORGANIZATION — not from a team, and not a role
   * demotion. Sits behind a persistent confirmation because it deletes their
   * memberships and org account access.
   */
  onRemoveMember?: (member: OrgMember) => Promise<unknown> | void;
  /** Roles the caller may assign (GET /admin/users/roles), ceiling-filtered server-side. */
  availableRoles?: string[];
  /**
   * The org's auto-created default team (`{org_id}-team-default`).
   *
   * Drives the "unassigned" warning: post-backfill, an unassigned person HOLDS a
   * membership row on this team rather than none, so warning only on an empty
   * membership list missed essentially everybody it was for (PR #4936 review, M3).
   */
  defaultTeamId?: string;
  /**
   * True when the org-wide team list was cut at its page cap, so the assignment
   * picker is not offering every team (PR #4936 review, M4).
   */
  teamsTruncated?: boolean;
  isLoading?: boolean;
  canManage?: boolean;
}

/** Mirrors `UserList`'s fallback for when the roles read is unavailable. */
const FALLBACK_ROLES: string[] = [
  AdminRole.MEMBER,
  AdminRole.DEPT_ADMIN,
  AdminRole.ORG_ADMIN,
  AdminRole.PLATFORM_ADMIN,
];

/** Fraction of the limit at which the usage bar turns amber. */
const NEAR_CAP_RATIO = 0.9;

export function MemberList({
  members,
  membershipsByUserId,
  teams,
  budgets,
  onAddTeam,
  onSetPrimary,
  onRemoveTeam,
  onChangeRole,
  onRemoveMember,
  availableRoles,
  defaultTeamId,
  teamsTruncated,
  isLoading,
  canManage = false,
}: MemberListProps) {
  const [managing, setManaging] = useState<OrgMember | null>(null);
  const [removing, setRemoving] = useState<OrgMember | null>(null);
  const [teamToAdd, setTeamToAdd] = useState('');
  const [isSubmitting, setIsSubmitting] = useState(false);
  /** Which member's role select is in flight — disables just that row's control. */
  const [roleUpdating, setRoleUpdating] = useState<string | null>(null);

  const roleOptions = (availableRoles?.length ? availableRoles : FALLBACK_ROLES).map((role) => ({
    value: role,
    label: formatRole(role),
  }));

  const teamName = (teamId: string) => teams.find((t) => t.id === teamId)?.name ?? teamId;
  const memberships = (member: OrgMember) => membershipsByUserId[member.id] ?? [];

  const closeModal = () => {
    if (isSubmitting) return;
    setManaging(null);
    setTeamToAdd('');
  };

  // One submit wrapper for all three writes: each keeps the dialog open on
  // rejection so the caller's banner is read in context, and none mutates a local
  // copy — the caller refetches, because a write can move the primary (removing
  // the primary promotes the oldest remaining membership server-side).
  const submit = async (action: () => Promise<unknown> | void, onSuccess?: () => void) => {
    setIsSubmitting(true);
    try {
      await action();
      onSuccess?.();
    } catch {
      // Left open on failure; the caller surfaces the server's message.
    } finally {
      setIsSubmitting(false);
    }
  };

  // The role select's submit. No local mutation on success OR failure: the select is
  // controlled by `member.role`, so a refused change (403, "cannot change your own
  // role") simply snaps back when the caller's refetch re-renders the row, and the
  // caller's banner carries the server's reason.
  const changeRole = async (member: OrgMember, role: string) => {
    if (!onChangeRole || role === (member.role ?? '')) return;
    setRoleUpdating(member.id);
    try {
      await onChangeRole(member, role);
    } catch {
      // The caller surfaces the server's message.
    } finally {
      setRoleUpdating(null);
    }
  };

  const columns: Column<OrgMember>[] = [
    {
      key: 'person',
      header: 'Person',
      render: (member) => (
        <div>
          <div className="font-medium text-gray-900 dark:text-white">
            {member.name || member.email}
          </div>
          {/* GitHub-ID-first labelling: people are recognized by their GitHub login
              where one is linked. Absence is stated, not left blank — "no GitHub
              linked" is a legitimate permanent state for an email signup, and a
              blank cell reads as data that failed to load. */}
          {member.githubUsername ? (
            <div className="font-mono text-xs text-gray-500 dark:text-gray-400">
              {member.githubUsername}
            </div>
          ) : (
            <div className="text-xs italic text-gray-400 dark:text-gray-500">no GitHub linked</div>
          )}
        </div>
      ),
    },
    {
      key: 'teams',
      header: 'Teams',
      render: (member) => {
        const rows = memberships(member);
        if (rows.length === 0) {
          return (
            <span className="text-xs text-amber-600 dark:text-amber-400">
              on no team — assign one
            </span>
          );
        }
        // Post-backfill an unassigned person is not row-LESS: lazy materialization
        // gives everybody a row on the org's default team, so "only the default
        // team" IS the unassigned state the mockup warns about. The chip still
        // renders (the membership is real); the warning rides alongside it.
        const onlyDefaultTeam =
          defaultTeamId !== undefined && rows.every((m) => m.teamId === defaultTeamId);
        return (
          <div className="flex flex-wrap items-center gap-1">
            {/* One chip per membership ROW. The count here is the count of rows in
                the membership table, so a person on two teams shows two chips —
                the single-valued pointer could only ever have shown one. */}
            {rows.map((membership) => (
              <span
                key={membership.teamId}
                className="inline-flex items-center gap-1 rounded bg-gray-100 px-2 py-0.5 text-xs text-gray-800 dark:bg-gray-700 dark:text-gray-100"
                data-testid={`team-chip-${membership.teamId}`}
              >
                {teamName(membership.teamId)}
                {membership.isPrimary && (
                  <Badge variant="info" size="sm">
                    primary
                  </Badge>
                )}
              </span>
            ))}
            {onlyDefaultTeam && (
              <span
                className="text-xs text-amber-600 dark:text-amber-400"
                data-testid={`unassigned-warning-${member.id}`}
              >
                ⚠ unassigned — default team
              </span>
            )}
          </div>
        );
      },
    },
    {
      key: 'role',
      header: 'Role',
      render: (member) =>
        canManage && onChangeRole ? (
          // A per-row SELECT, per the mockup — the same act as UserList's "Change
          // Role" dialog, wired to the same client call by the page. Affordance
          // only: the server refuses roles above the caller's ceiling and
          // self-changes, and the refusal surfaces via the caller's banner. The
          // member's CURRENT role is always an option even when the ceiling-filtered
          // list omits it (an org admin viewing a platform admin), so the control
          // never renders blank — the server refuses a change either way.
          <Select
            name={`member-role-${member.id}`}
            aria-label={`Role for ${member.name || member.email}`}
            value={member.role ?? AdminRole.MEMBER}
            onChange={(e) => changeRole(member, e.target.value)}
            options={
              member.role && !roleOptions.some((o) => o.value === member.role)
                ? [{ value: member.role, label: formatRole(member.role) }, ...roleOptions]
                : roleOptions
            }
            disabled={roleUpdating === member.id}
            className="max-w-[11rem] py-1 text-sm"
          />
        ) : (
          <span className="text-sm text-gray-600 dark:text-gray-400">
            {formatRole(member.role) || '-'}
          </span>
        ),
    },
  ];

  if (budgets) {
    columns.push({
      key: 'spend',
      header: 'Spend / limit (month)',
      render: (member) => <SpendCell budget={budgets[member.id]} />,
    });
  }

  if (canManage) {
    columns.push({
      key: 'actions',
      header: '',
      align: 'right',
      render: (member) => (
        <div className="flex items-center justify-end gap-2">
          <Button variant="ghost" size="sm" onClick={() => setManaging(member)}>
            Teams
          </Button>
          {onRemoveMember && (
            <Button
              variant="ghost"
              size="sm"
              className="text-red-600 hover:text-red-700 dark:text-red-400"
              onClick={() => setRemoving(member)}
            >
              Remove
            </Button>
          )}
        </div>
      ),
    });
  }

  // Teams the member is not already on — the picker's options. Filtering here is a
  // convenience, not a rule: the server is idempotent on (user, team), so offering
  // a team twice would be harmless, just confusing.
  const assignable = managing
    ? teams.filter((team) => !memberships(managing).some((m) => m.teamId === team.id))
    : [];
  const managingRows = managing ? memberships(managing) : [];

  return (
    <>
      <Card padding="none">
        <div className="border-b border-gray-200 p-4 dark:border-gray-700">
          <h3 className="font-semibold text-gray-900 dark:text-white">Members</h3>
        </div>
        <Table
          columns={columns}
          data={members}
          keyExtractor={(member) => member.id}
          isLoading={isLoading}
          emptyMessage="No members in this organization yet."
        />
        <p className="border-t border-gray-200 p-4 text-xs text-gray-500 dark:border-gray-700 dark:text-gray-400">
          People are recognized by their GitHub ID when one is linked. A person can be on
          several teams; budgets and Bedrock routing follow their primary team.
        </p>
      </Card>

      {/* Team management for one member. All three writes live in one dialog because
          they are one decision — "which teams is this person on, and which one is
          primary" — and splitting them across three modals hid the constraint that
          only one may be primary. */}
      <Modal
        isOpen={managing !== null}
        onClose={closeModal}
        title={managing ? `Teams for ${managing.name || managing.email}` : 'Teams'}
      >
        <div className="space-y-4">
          <div className="space-y-2">
            {managingRows.length === 0 ? (
              <p className="text-sm text-gray-600 dark:text-gray-400">
                This person is on no team yet.
              </p>
            ) : (
              managingRows.map((membership) => (
                <div
                  key={membership.teamId}
                  className="flex items-center justify-between gap-2 rounded border border-gray-200 px-3 py-2 dark:border-gray-700"
                  data-testid={`managed-team-${membership.teamId}`}
                >
                  <span className="text-sm text-gray-900 dark:text-white">
                    {teamName(membership.teamId)}
                    {membership.isPrimary && (
                      <Badge variant="info" size="sm" className="ml-2">
                        primary
                      </Badge>
                    )}
                  </span>
                  <span className="space-x-2">
                    {/* Only offered for non-primary rows: "make primary" on the team
                        that already is primary is the request the server answers with
                        the 409 the panel exists to avoid provoking pointlessly. */}
                    {onSetPrimary && !membership.isPrimary && (
                      <Button
                        variant="ghost"
                        size="sm"
                        disabled={isSubmitting}
                        onClick={() => submit(() => onSetPrimary(managing!, membership.teamId))}
                      >
                        Make primary
                      </Button>
                    )}
                    {onRemoveTeam && (
                      <Button
                        variant="ghost"
                        size="sm"
                        disabled={isSubmitting}
                        onClick={() => submit(() => onRemoveTeam(managing!, membership.teamId))}
                      >
                        Remove
                      </Button>
                    )}
                  </span>
                </div>
              ))
            )}
          </div>

          {onAddTeam && (
            <>
              <div className="flex items-end gap-2 border-t border-gray-200 pt-4 dark:border-gray-700">
                <Select
                  name="team-to-add"
                  label="Add to team"
                  value={teamToAdd}
                  onChange={(e) => setTeamToAdd(e.target.value)}
                  options={assignable.map((team) => ({ value: team.id, label: team.name }))}
                  placeholder="Select a team…"
                  className="flex-1"
                />
                <Button
                  disabled={!teamToAdd}
                  isLoading={isSubmitting}
                  onClick={() =>
                    submit(
                      () => onAddTeam(managing!, teamToAdd),
                      () => setTeamToAdd('')
                    )
                  }
                >
                  Add
                </Button>
              </div>
              {teamsTruncated && (
                // Disclosed, not silent (M4): a picker showing page 1 of a larger
                // team list reads as the complete set, and the team someone is
                // looking for being absent reads as "does not exist".
                <p className="text-xs text-amber-600 dark:text-amber-400" data-testid="teams-truncated-warning">
                  Not every team is listed — this organization has more teams than one
                  page shows.
                </p>
              )}
            </>
          )}

          <p className="text-xs text-gray-500 dark:text-gray-400">
            Budgets and Bedrock routing follow the primary team. Adding a team does not move
            the primary — use “Make primary” for that.
          </p>

          <ModalFooter>
            <Button variant="secondary" onClick={closeModal} disabled={isSubmitting}>
              Done
            </Button>
          </ModalFooter>
        </div>
      </Modal>

      {/* Persistent confirmation, per the panel's convention (the department-delete
          modal is the sibling). The copy states the real consequence: this is
          removal from the ORGANIZATION — a deletion, not UserList's demote-to-member
          — and a rejected promise keeps it open so the server's refusal is read in
          context. */}
      <Modal
        isOpen={removing !== null}
        onClose={() => {
          if (!isSubmitting) setRemoving(null);
        }}
        title="Remove member"
        size="sm"
      >
        <div className="space-y-4">
          <p className="text-sm text-gray-700 dark:text-gray-300">
            Remove <strong>{removing?.name || removing?.email}</strong> from this
            organization? This deletes their team memberships and their account access for
            this org. It is not a role change, and it cannot be undone.
          </p>
          <ModalFooter>
            <Button variant="secondary" onClick={() => setRemoving(null)} disabled={isSubmitting}>
              Cancel
            </Button>
            <Button
              variant="danger"
              isLoading={isSubmitting}
              onClick={() =>
                submit(
                  () => onRemoveMember?.(removing!),
                  () => setRemoving(null)
                )
              }
            >
              Remove member
            </Button>
          </ModalFooter>
        </div>
      </Modal>
    </>
  );
}

/**
 * One member's month spend against the limit that governs them.
 *
 * The three states are distinct on purpose (contract rule 2, #4620/#4690): a figure
 * against a limit, a figure against NO limit, and no figures at all. Only the first
 * draws a bar, because a bar needs a denominator.
 */
function SpendCell({ budget }: { budget?: MemberBudget }) {
  if (!budget) {
    return <span className="text-xs text-gray-400 dark:text-gray-500">—</span>;
  }

  const spend = Number(budget.spendUsd);
  const limit = budget.limitUsd === null ? null : Number(budget.limitUsd);

  if (limit === null || !Number.isFinite(limit) || limit <= 0) {
    // No governing limit at any rung — stated as such. `"$0.00 / $0.00"` here would
    // describe a person forbidden to spend anything, which is the inverse of the
    // truth, and is the defect contract rule 2 exists to prevent.
    return (
      <div>
        <div className="text-sm text-gray-900 dark:text-white">{formatUsd(spend)}</div>
        <div className="mt-0.5 text-xs text-gray-500 dark:text-gray-400">
          {budget.sourceLabel || 'no limit'}
        </div>
      </div>
    );
  }

  const ratio = limit > 0 ? spend / limit : 0;
  const nearCap = ratio >= NEAR_CAP_RATIO;

  return (
    <div>
      <div
        className={`text-sm ${nearCap ? 'font-medium text-amber-700 dark:text-amber-400' : 'text-gray-900 dark:text-white'}`}
      >
        {formatUsd(spend)} / {formatUsd(limit)}
      </div>
      <div className="mt-1 h-1.5 w-24 rounded-full bg-gray-100 dark:bg-gray-700">
        <div
          className={`h-1.5 rounded-full ${nearCap ? 'bg-amber-500' : 'bg-primary-500'}`}
          // Clamped at 100% so an over-cap member's bar stays inside its track
          // rather than overflowing the cell — the amber figure above already says
          // they are over.
          style={{ width: `${Math.min(100, Math.round(ratio * 100))}%` }}
        />
      </div>
      {/* Verbatim from the server: "individual limit", "team default", "org
          default", "platform default" — third-person labels composed there for this
          surface, because only the ladder resolver knows which rung won. */}
      {budget.sourceLabel && (
        <div className="mt-0.5 text-xs text-gray-500 dark:text-gray-400">
          {budget.sourceLabel}
        </div>
      )}
    </div>
  );
}

/** Whole dollars, matching the mockup's `$38 / $75`. */
function formatUsd(value: number): string {
  if (!Number.isFinite(value)) return '—';
  return `$${value.toLocaleString('en-US', { maximumFractionDigits: 2 })}`;
}

function formatRole(role: string | null): string {
  if (!role) return '';
  return role.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
}
