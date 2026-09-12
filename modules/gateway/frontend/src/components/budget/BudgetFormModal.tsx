/**
 * Budget Form Modal
 *
 * Issue #185: Budget & Rate Limit Management UI for Org Admins
 * Issue #220: Fix Admin UI Budget/RateLimit CRUD + Organization Page for Org Admins
 * Modal form for creating and editing budget configurations.
 *
 * Issue #4687 made this form author TWO different things, and the distinction is the
 * point of the issue rather than an implementation detail:
 *
 * - Every entity type here writes a **budget config**, scoped to the organization it
 *   was authored in (`POST /admin/organizations/{orgId}/budgets`).
 * - "Person limit — all workspaces" writes a **person cap**, a partition-free row
 *   keyed by the cross-workspace person anchor (`PUT /budget/person-cap/{anchor}`),
 *   and is platform-admin-only per the ruling on #4620 §4.2.
 *
 * Before this, only the first existed, while its "User — cloud agents" option read
 * like the second — so admins who wanted to bound a person's total agent spend
 * authored a cap on whatever fraction of it billed to the workspace they happened to
 * be in, and found out weeks later from spend that never stopped. Both paths are
 * reachable from one form because that is where the choice actually gets made; they
 * are kept visibly distinct in the labels, the help text, and the post-create advisory.
 */

import { useState, useEffect } from 'react';
import { Modal, ModalFooter } from '@/components/ui/Modal';
import { Alert } from '@/components/ui/Alert';
import { Button } from '@/components/ui/Button';
import { Input } from '@/components/ui/Input';
import { Select } from '@/components/ui/Select';
import { EntitySelector } from '@/components/shared/EntitySelector';
import { useToast } from '@/contexts/ToastContext';
import { createBudget, updateBudget } from '@/services/budget';
import { getMemberGithubUserId } from '@/services/admin';
import { setPersonCapFor } from '@/services/personCap';
import { EntityType, PeriodType, EnforcementMode } from '@/types';
import type { BudgetPeriodType } from '@/types/budget';
import {
  formatEntityType,
  PERSON_LIMIT_LABEL,
  PERSON_LIMIT_OPTION_VALUE,
  WORKSPACE_NOUN,
} from '@/utils/entityLabels';

interface BudgetFormData {
  entityType: string;
  entityId: string;
  periodType: string;
  budgetAmountUsd: number;
  enforcementMode: string;
}

interface BudgetFormModalProps {
  isOpen: boolean;
  onClose: () => void;
  /**
   * Called once the write has landed.
   *
   * The optional `advisory` (#4669, surfaced by #4687) is a sentence about the cap that
   * was just **created**, never a reason it was refused — this callback firing at all
   * means the row is committed. Passed up because the create closes this modal, so a
   * notice rendered here would vanish with it. Callers that do not care may ignore the
   * argument; the existing no-arg signature still type-checks.
   */
  onSuccess: (result?: { advisory: string | null; entityId?: string }) => void;
  /**
   * Preselection for the create flow (review fix on #4688): the advisory's
   * "Set a person limit instead" must land on the person-limit form with the
   * person already picked — not on the default Team form, where the operator
   * would have to rediscover both and may re-author the mis-aimed cap instead.
   */
  preset?: { entityType: string; entityId: string } | null;
  orgId: string;
  editData?: BudgetFormData;
  /**
   * Whether the signed-in caller is a platform admin — Issue #4687.
   *
   * Gates the person-limit option only. The ruling on #4620 §4.2 permits exactly two
   * parties to author a person's cross-workspace limit: the person themselves (the
   * self-service card on `/budget`) and a platform admin, who holds cross-org authority
   * by design. An org admin never can, including for a member of their own org, because
   * the row governs that person's spend in tenants the org admin has no membership in.
   *
   * This flag hides a dead end; it is NOT the access control. `require_platform_admin`
   * on the route is, and it answers `403` regardless of what this form renders.
   */
  isPlatformAdmin?: boolean;
}

/**
 * The person anchor for a picked member, or `null` when they have no GitHub identity.
 *
 * Resolved from the server (`getMemberGithubUserId`) at submit time rather than built
 * from anything on the picker's payload. The anchor is the key the cap is stored under,
 * so an id inferred client-side from a `users.id`, an email or a display name yields a
 * row that validates, shows a limit, and is never matched by enforcement — #4511 one
 * ledger over, which is the failure this whole issue exists to stop reproducing.
 */
async function resolvePersonAnchor(userId: string): Promise<string | null> {
  const githubUserId = await getMemberGithubUserId(userId);
  return githubUserId ? `github:${githubUserId}` : null;
}

const periodTypeOptions = [
  { value: PeriodType.DAILY, label: 'Daily' },
  { value: PeriodType.WEEKLY, label: 'Weekly' },
  { value: PeriodType.MONTHLY, label: 'Monthly' },
];

const enforcementModeOptions = [
  { value: EnforcementMode.HARD, label: 'Hard (Block requests when exceeded)' },
  { value: EnforcementMode.SOFT, label: 'Soft (Warn but allow requests)' },
];

/** Server detail from either error shape the client produces (review fix on #4688). */
function extractApiMessage(error: unknown): string | null {
  if (error instanceof Error) return error.message || null;
  if (error && typeof error === 'object') {
    const body = error as { message?: unknown; detail?: unknown };
    if (typeof body.message === 'string' && body.message) return body.message;
    if (typeof body.detail === 'string' && body.detail) return body.detail;
  }
  return null;
}

export function BudgetFormModal({
  isOpen,
  onClose,
  onSuccess,
  preset,
  orgId,
  editData,
  isPlatformAdmin = false,
}: BudgetFormModalProps) {
  const toast = useToast();
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [formData, setFormData] = useState<BudgetFormData>({
    entityType: EntityType.TEAM,
    entityId: '',
    periodType: PeriodType.MONTHLY,
    budgetAmountUsd: 0,
    enforcementMode: EnforcementMode.HARD,
  });
  /**
   * "This person has no linked GitHub identity", so no anchor exists to key a cap on.
   *
   * A pre-submit stop, and the one case where the person-limit path declines to write:
   * there is no cross-workspace key for this person, so any cap authored would be inert.
   * Distinct from an error — nothing failed, and nothing was created.
   */
  const [unlinkedPerson, setUnlinkedPerson] = useState(false);
  /**
   * The org partition this budget is WRITTEN to — Issue #4948.
   *
   * Not always `orgId`: the entity picker now lists every org the caller administers,
   * and a budget for another org's team must land in THAT org's partition. Enforcement
   * matches on `(org_id, entity_type, entity_id)`, so posting a `sophos-it` team to the
   * caller's own partition would store a row that reads back "capped" and is never
   * matched — the #4511 inert-config class one dimension over.
   *
   * Seeded from and reset to `orgId`, so a single-org admin's behaviour is unchanged.
   */
  const [scopeOrgId, setScopeOrgId] = useState(orgId);

  const isEditMode = !!editData;
  // A person limit is not an entity type; this option routes to a different API. It can
  // only ever be selected when `isPlatformAdmin` — EntitySelector does not render the
  // option otherwise — but the check is written against the selection rather than the
  // role so the submit path cannot diverge from what the form displays.
  const isPersonLimit = !isEditMode && formData.entityType === PERSON_LIMIT_OPTION_VALUE;

  // Reset form when modal opens/closes or editData changes
  useEffect(() => {
    setUnlinkedPerson(false);
    // Reopening the form must not inherit the org picked during the last create
    // (#4948) — that would silently author the next budget in a partition the
    // operator is no longer looking at.
    setScopeOrgId(orgId);
    if (editData) {
      setFormData(editData);
    } else {
      setFormData({
        entityType: preset?.entityType ?? EntityType.TEAM,
        entityId: preset?.entityId ?? '',
        periodType: PeriodType.MONTHLY,
        budgetAmountUsd: 0,
        enforcementMode: EnforcementMode.HARD,
      });
    }
  }, [editData, isOpen, preset, orgId]);

  // The unlinked-person notice describes ONE person at the moment of one submit.
  // Cleared the moment the operator picks a different person or a different kind
  // (review fix on #4688): left standing, it asserts the NEXT person is unlinked
  // too, or sits over a Team form describing nobody on it.
  useEffect(() => {
    setUnlinkedPerson(false);
  }, [formData.entityId, formData.entityType]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();

    if (!formData.entityId.trim()) {
      toast.error(isPersonLimit ? 'Select a person' : 'Entity ID is required');
      return;
    }

    if (formData.budgetAmountUsd <= 0) {
      toast.error('Budget amount must be greater than 0');
      return;
    }

    setIsSubmitting(true);
    // Cleared per attempt, never accumulated: a stale notice beside a different
    // submission would describe a person the operator is no longer looking at.
    setUnlinkedPerson(false);

    try {
      if (isPersonLimit) {
        // The anchor is resolved from the server for the person who was PICKED. If
        // they have no linked GitHub identity there is no cross-workspace key a cap
        // could be stored against, so nothing is submitted — the same explanation the
        // self-service card gives, rather than a 422 from an anchor we knew was bad.
        const anchor = await resolvePersonAnchor(formData.entityId);
        if (!anchor) {
          setUnlinkedPerson(true);
          return;
        }
        // A string at the column's precision, matching the self path: money must not
        // cross the wire as a JS number.
        await setPersonCapFor(
          anchor,
          formData.periodType as BudgetPeriodType,
          formData.budgetAmountUsd.toFixed(2)
        );
        toast.success('Person limit set successfully');
        onSuccess({ advisory: null, entityId: formData.entityId });
        return;
      }

      if (isEditMode) {
        // For edit, we use the PUT endpoint for budget config by entity
        await updateBudget(
          orgId,
          formData.entityType,
          formData.entityId,
          {
            budget_amount_usd: formData.budgetAmountUsd,
            enforcement_mode: formData.enforcementMode as EnforcementMode,
          }
        );
        toast.success('Budget updated successfully');
      } else {
        // `scopeOrgId`, NOT `orgId` (#4948): the partition must be the org the entity
        // belongs to, or the row matches nothing at request time.
        const created = await createBudget(scopeOrgId, {
          entity_type: formData.entityType as EntityType,
          entity_id: formData.entityId,
          period_type: formData.periodType as PeriodType,
          budget_amount_usd: formData.budgetAmountUsd,
          enforcement_mode: formData.enforcementMode as EnforcementMode,
        });
        toast.success('Budget created successfully');
        // The advisory is handed to the page rather than rendered here, because the
        // create closes this modal — a notice owned by a dialog that is going away
        // would be shown and immediately dismissed. The cap EXISTS either way; this is
        // reporting, not a branch on success.
        onSuccess({ advisory: created.advisory ?? null, entityId: formData.entityId });
        return;
      }
      onSuccess();
    } catch (error: unknown) {
      const message =
        // The API client throws the PARSED ERROR BODY (a plain object), not an
        // Error — `instanceof` matching only Error dropped every real server
        // detail and blamed the wrong operation (review fix on #4688).
        extractApiMessage(error) ?? (isPersonLimit ? 'Failed to set the person limit' : `Failed to ${isEditMode ? 'update' : 'create'} budget`);
      toast.error(message);
    } finally {
      setIsSubmitting(false);
    }
  };

  const handleClose = () => {
    if (!isSubmitting) {
      onClose();
    }
  };

  return (
    <Modal
      isOpen={isOpen}
      onClose={handleClose}
      title={isEditMode ? 'Edit Budget' : isPersonLimit ? 'Set Person Limit' : 'Create Budget'}
      size="md"
    >
      <form onSubmit={handleSubmit} className="space-y-4">
        {isEditMode ? (
          <>
            {/* In edit mode, show read-only entity info */}
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Entity Type
              </label>
              {/* The friendly label, not the raw wire value — editing a cloud-agent
                  budget must not be where `root_user` leaks onto the screen (#4536). */}
              <div className="w-full px-3 py-2 border rounded-lg border-gray-300 dark:border-gray-600 bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-300">
                {formatEntityType(formData.entityType)}
              </div>
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Entity ID
              </label>
              <div className="w-full px-3 py-2 border rounded-lg border-gray-300 dark:border-gray-600 bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-300">
                {formData.entityId}
              </div>
            </div>
          </>
        ) : (
          <EntitySelector
            orgId={orgId}
            entityType={formData.entityType}
            entityId={formData.entityId}
            onEntityTypeChange={(entityType) => setFormData((prev) => ({ ...prev, entityType, entityId: '' }))}
            onEntityIdChange={(entityId) => setFormData((prev) => ({ ...prev, entityId }))}
            // Issue #4948: redirect the WRITE to the org the operator picked. Passing
            // this is what makes the org picker appear at all — the component withholds
            // it from consumers that cannot honour it, so the affordance and the
            // correct partition ship together or not at all.
            onScopeOrgChange={setScopeOrgId}
            disabled={false}
            // Issue #4536: budgets are the one surface where a per-person cloud-agent
            // cap is real — the create/update path resolves and persists the type, and
            // the /budget dashboard already displays it.
            allowCloudAgentScope
            // Issue #4687: platform admins only — the ruling on #4620 §4.2. The route
            // is the boundary; this is the affordance that keeps org admins out of a 403.
            allowPersonLimitScope={isPlatformAdmin}
          />
        )}

        {/* Nothing was written: this person has no cross-workspace identity, so any
            limit authored for them would be a row enforcement can never match. Rendered
            as a calm explanation rather than an error, mirroring the self-service card —
            it is a permanent, legitimate state for a member who signed up by email, and
            a red failure with a retry that can never succeed reads as an outage. */}
        {isPersonLimit && unlinkedPerson && (
          <Alert variant="info" title="This person has no linked GitHub identity">
            A {PERSON_LIMIT_LABEL.toLowerCase()} follows a person across {WORKSPACE_NOUN}s
            using their linked GitHub identity, and this member has none — so there is no
            cross-{WORKSPACE_NOUN} identity a limit could be attached to. No limit was
            set. Once they sign in with GitHub, you can set one.
          </Alert>
        )}

        <Select
          label="Period Type"
          options={periodTypeOptions}
          value={formData.periodType}
          onChange={(e) => setFormData({ ...formData, periodType: e.target.value })}
          disabled={isEditMode}
          required
        />

        <Input
          label="Budget Amount (USD)"
          type="number"
          min="0.01"
          step="0.01"
          value={formData.budgetAmountUsd || ''}
          onChange={(e) =>
            setFormData({ ...formData, budgetAmountUsd: parseFloat(e.target.value) || 0 })
          }
          placeholder="e.g., 500.00"
          required
        />

        {/* Issue #4687: no enforcement-mode picker for a person limit. The mode is not
            client-settable on that route — the server writes `hard` on every PUT
            (#4630), because authoring the limit IS the choice to be enforced. Offering
            the control would let an admin believe they had authored a soft limit and
            get an enforcing one, which is the screen/behavior disagreement #4620 exists
            to close. Stated instead of hidden, so the consequence is not a surprise. */}
        {isPersonLimit ? (
          <p className="text-sm text-gray-600 dark:text-gray-400" data-testid="person-limit-enforcement-note">
            A person limit is enforced: once this person's total agent spend across every{' '}
            {WORKSPACE_NOUN} passes it, their agent runs are stopped until the period
            resets or the limit is raised.
          </p>
        ) : (
          <Select
            label="Enforcement Mode"
            options={enforcementModeOptions}
            value={formData.enforcementMode}
            onChange={(e) => setFormData({ ...formData, enforcementMode: e.target.value })}
            required
          />
        )}

        <ModalFooter>
          <Button type="button" variant="secondary" onClick={handleClose} disabled={isSubmitting}>
            Cancel
          </Button>
          <Button type="submit" isLoading={isSubmitting}>
            {isEditMode ? 'Save Changes' : isPersonLimit ? 'Set Person Limit' : 'Create Budget'}
          </Button>
        </ModalFooter>
      </form>
    </Modal>
  );
}
