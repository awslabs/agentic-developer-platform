/**
 * Entity Selector Component
 *
 * Issue #220: Fix Admin UI Budget/RateLimit CRUD + Organization Page for Org Admins
 * Issue #226: Updated to use Cognito-backed endpoints as single source of truth.
 *
 * A shared component for selecting entity type and entity ID when creating/editing
 * budgets and rate limits. Now fetches entities from Cognito via the backend API.
 */

import { useState, useEffect } from 'react';
import { Select } from '@/components/ui/Select';
import { Input } from '@/components/ui/Input';
import {
  getOrgUsers,
  getCognitoTeams,
  getCognitoDepartments,
} from '@/services/admin';
import { EntityType } from '@/types';
import {
  formatEntityType,
  entityTypeHelpText,
  PERSON_LIMIT_LABEL,
  PERSON_LIMIT_OPTION_VALUE,
} from '@/utils/entityLabels';

interface EntityOption {
  value: string;
  label: string;
  /** Rendered but unselectable — see the NULL-sub case in the user branch below. */
  disabled?: boolean;
}

interface EntitySelectorProps {
  orgId: string;
  entityType: string;
  entityId: string;
  onEntityTypeChange: (entityType: string) => void;
  onEntityIdChange: (entityId: string) => void;
  disabled?: boolean;
  /** List of department IDs for fetching teams (needed since teams require dept ID) */
  departmentIds?: string[];
  /**
   * Offer "User — cloud agents" (`root_user`) alongside "User — direct use".
   *
   * Issue #4536. Opt-in rather than always-on because this component is shared with
   * the rate-limit form, and nothing enforces a `root_user` rate limit — offering it
   * there would let an operator configure a limit that silently does nothing. Only
   * the budget form, whose create/update path resolves and persists the type, sets it.
   */
  allowCloudAgentScope?: boolean;
  /**
   * Offer "Person limit — all workspaces" alongside the workspace-scoped kinds.
   *
   * Issue #4687. Opt-in for a stronger reason than `allowCloudAgentScope`'s: this
   * option is **platform-admin only** per the ruling on #4620 §4.2, and the caller is
   * the only party that knows the caller's role. Defaulting it on would offer org
   * admins an action that 403s — and defaulting it *off* means a surface that forgets
   * to pass it loses a feature rather than leaking authority, which is the correct
   * direction for the mistake to fall.
   *
   * The gate here is an affordance, never the boundary:
   * `PUT /budget/person-cap/{anchor}` enforces `require_platform_admin` server-side
   * regardless of what this component renders.
   *
   * Selecting it does NOT change which `entity_type` is submitted — a person limit is
   * not an entity type. It selects a different API. See `PERSON_LIMIT_OPTION_VALUE`.
   */
  allowPersonLimitScope?: boolean;
}

/** The entity types every caller offers, in narrowing order. */
const baseEntityTypes = [
  EntityType.ORGANIZATION,
  EntityType.DEPARTMENT,
  EntityType.TEAM,
  EntityType.USER,
];

/**
 * Every page of the org's members, not just the first (review fix on #4688).
 *
 * The pickers built on this are the ONLY path to a person for the person-scoped
 * kinds — the person limit deliberately has no typed fallback — so a single
 * 100-row page silently made members #101+ un-cappable with nothing on screen
 * saying why. Bounded at 10 pages (1,000 members) to keep a runaway org from
 * hanging the modal; beyond that the picker is the wrong tool and search is the
 * follow-up.
 */
async function getAllOrgUsers(orgId: string) {
  const items = [];
  let page = 1;
  for (; page <= 10; page++) {
    const response = await getOrgUsers(orgId, { pageSize: 100, page });
    items.push(...response.items);
    if (!response.hasMore) return { items, truncated: false };
  }
  return { items, truncated: true };
}

export function EntitySelector({
  orgId,
  entityType,
  entityId,
  onEntityTypeChange,
  onEntityIdChange,
  disabled = false,
  allowCloudAgentScope = false,
  allowPersonLimitScope = false,
}: EntitySelectorProps) {
  // Labels come from the shared map, so this dropdown cannot word the two
  // person-scoped kinds differently from the list that renders them (#4536).
  // Without the cloud-agent kind on offer (the rate-limit form), "User — direct
  // use" would imply a cloud-agents counterpart that doesn't exist there, so
  // that surface keeps the plain "User" label and no bucket help text.
  const entityTypeOptions = [
    ...(allowCloudAgentScope ? [...baseEntityTypes, EntityType.ROOT_USER] : baseEntityTypes).map(
      (value) => ({
        value: value as string,
        label:
          !allowCloudAgentScope && value === EntityType.USER ? 'User' : formatEntityType(value),
      })
    ),
    // Last in the list, after the workspace-scoped kinds it is the alternative to
    // (#4687). Its label is not in ENTITY_TYPE_LABELS because it is not an entity
    // type — see PERSON_LIMIT_LABEL.
    ...(allowPersonLimitScope
      ? [{ value: PERSON_LIMIT_OPTION_VALUE, label: PERSON_LIMIT_LABEL }]
      : []),
  ];
  // Whether the cross-workspace person limit is the selected kind. It changes two
  // things: the picker becomes mandatory (no typed anchors, #4687) and the "Entity ID"
  // label becomes a person label, because "Entity ID" for a row keyed by a person is
  // the ledger vocabulary #4536 exists to keep off the screen.
  const isPersonLimit = entityType === PERSON_LIMIT_OPTION_VALUE;
  const [entityOptions, setEntityOptions] = useState<EntityOption[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [useManualInput, setUseManualInput] = useState(false);
  // Bumped by the person-limit failure notice's Retry button (review fix on
  // #4688): the fetch effect otherwise re-runs only on entityType/orgId change,
  // so the old copy's "try again in a moment" pointed at nothing.
  const [fetchNonce, setFetchNonce] = useState(0);

  // Fetch entities when entity type or org changes
  // Issue #226: Updated to use Cognito-backed endpoints
  useEffect(() => {
    if (!orgId) return;

    let cancelled = false;

    async function fetchEntities() {
      setIsLoading(true);
      setError(null);
      setEntityOptions([]);
      setUseManualInput(false);

      try {
        let options: EntityOption[] = [];

        switch (entityType) {
          case EntityType.ORGANIZATION:
            options = [{ value: orgId, label: `Current Organization (${orgId})` }];
            break;

          case EntityType.DEPARTMENT: {
            const deptResponse = await getCognitoDepartments(orgId);
            if (cancelled) return;
            options = deptResponse.items.map((dept) => ({
              value: dept.departmentId,
              label: dept.departmentId,
            }));
            break;
          }

          case EntityType.TEAM: {
            const teamsResponse = await getCognitoTeams(orgId, { pageSize: 100 });
            if (cancelled) return;
            options = teamsResponse.items.map((team) => ({
              value: team.groupName,
              label: team.description
                ? `${team.groupName} (${team.description})`
                : team.groupName,
            }));
            break;
          }

          case EntityType.USER: {
            // Issue #4511: this MUST send the Cognito sub. Budgets, rate limits
            // and the usage ledger all key `user` entities by the sub, so an id
            // in any other namespace produces a record the engine can never
            // match — a cap that appears configured but never enforces and never
            // shows spend. The previous source (`getCognitoUsers`) supplied the
            // Cognito *username*, which is `GitHub_<github_id>` for anyone
            // onboarded through GitHub and only coincidentally equals the sub for
            // email-signup users, which is why this survived testing.
            const usersResponse = await getAllOrgUsers(orgId);
            if (cancelled) return;
            options = usersResponse.items.map((user) => {
              const displayName = user.name ? `${user.name} (${user.email})` : user.email;
              // Members who have never signed in have no sub, so no budget for
              // them could ever be enforced. Show them disabled rather than
              // omitting them, so the absence is explained rather than confusing.
              return user.cognitoSub
                ? { value: user.cognitoSub, label: displayName }
                : {
                    value: user.id,
                    label: `${displayName} — has not signed in yet`,
                    disabled: true,
                  };
            });
            break;
          }

          // Issue #4687: the person limit reuses the SAME person picker as the two
          // budget kinds — one list of people, so the workspace-scoped option and the
          // cross-workspace one cannot disagree about who exists, which is the whole
          // reason an admin can compare them.
          //
          // The value stays the canonical `users.id`, NOT the person anchor: the
          // anchor's GitHub id is not on this payload and must be resolved from the
          // server (`admin.getMemberGithubUserId`) rather than guessed from anything
          // here. Building `github:<something>` in this component is exactly the
          // #4511 class — a key that validates and never matches. The consumer does
          // that resolution at submit time, once, for the one person picked.
          case PERSON_LIMIT_OPTION_VALUE:
          case EntityType.ROOT_USER: {
            // Issue #4536: the SAME person picker as direct-use above — one list of
            // people, so the two budget kinds cannot disagree about who exists. The
            // difference is which id is submitted: the cloud-agent ledger is keyed by
            // canonical `users.id` (#4300), not by Cognito sub, so sending the sub
            // here would recreate #4511 one ledger over — a cap that exists and
            // matches nothing.
            const usersResponse = await getAllOrgUsers(orgId);
            if (cancelled) return;
            options = usersResponse.items.map((user) => ({
              // Never disabled: the canonical id always exists, and agent spend is
              // attributed from the run's lineage rather than a signed-in session, so
              // a member who has not signed in yet still has an enforceable cloud
              // budget. That is the one asymmetry with direct-use.
              value: user.id,
              label: user.name ? `${user.name} (${user.email})` : user.email,
            }));
            break;
          }

          default:
            setUseManualInput(true);
            setIsLoading(false);
            return;
        }

        if (!cancelled) {
          setEntityOptions(options);
          setUseManualInput(options.length === 0);
        }
      } catch (err) {
        if (!cancelled) {
          console.warn('Failed to fetch entities, falling back to manual input:', err);
          setError('Failed to load entities. You can enter the ID manually.');
          setUseManualInput(true);
        }
      } finally {
        if (!cancelled) {
          setIsLoading(false);
        }
      }
    }

    fetchEntities();

    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [entityType, orgId, fetchNonce]);

  return (
    <div className="space-y-4">
      <Select
        label="Entity Type"
        options={entityTypeOptions}
        value={entityType}
        onChange={(e) => {
          onEntityTypeChange(e.target.value);
          onEntityIdChange(''); // Clear entity ID when type changes
        }}
        disabled={disabled}
        required
        // Which of a person's two spend buckets this cap governs, and — for the
        // workspace-scoped one — that it IS workspace-scoped plus what to use instead
        // (#4536, #4687). Without it the labels alone don't tell you which dollars land
        // where. Budget form only — see the option-label note above.
        helperText={
          allowCloudAgentScope || allowPersonLimitScope
            ? entityTypeHelpText(entityType)
            : undefined
        }
      />

      {isLoading ? (
        <div className="w-full">
          <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
            Entity ID
            <span className="text-red-500 ml-1">*</span>
          </label>
          <div className="w-full px-3 py-2 border rounded-lg border-gray-300 dark:border-gray-600 bg-gray-50 dark:bg-gray-700 text-gray-500 dark:text-gray-400">
            Loading entities...
          </div>
        </div>
      ) : isPersonLimit && (useManualInput || entityOptions.length === 0) ? (
        // Issue #4687: the person limit has NO manual-entry fallback, and that is a
        // requirement rather than a missing feature. Every other kind here accepts a
        // typed id because the server resolves it against this org's users and rejects
        // what it cannot match. A person limit is keyed by the cross-workspace anchor,
        // which is derived from a server-sourced GitHub identity for a person the
        // operator PICKED — so a text box here could only ever be a way to type an
        // anchor, which is the one thing #4511 says must not exist. When the member
        // list is unavailable there is nothing to pick from, so the answer is "not
        // now", not "type it".
        <div
          className="rounded-lg border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800 px-3 py-3 text-sm text-gray-600 dark:text-gray-400"
          data-testid="person-limit-no-picker"
        >
          {error ? (
            <>
              {`The member list could not be loaded, so there is nobody to pick. A ${PERSON_LIMIT_LABEL.toLowerCase()} must be set on a person chosen from this list — it cannot be typed in.`}{' '}
              <button
                type="button"
                className="underline font-medium hover:opacity-80"
                onClick={() => setFetchNonce((nonce) => nonce + 1)}
                data-testid="person-limit-retry"
              >
                Retry
              </button>
            </>
          ) : !orgId ? (
            // An org-less session cannot list anyone; saying "no members" would
            // misdiagnose the caller's own token as an empty org (review fix on
            // #4688).
            'Your session carries no GitHub org, so there is no member list to pick from. Open Budget Management from within a GitHub org context.'
          ) : (
            // Honest scope: the picker lists THIS org's members even though the
            // limit follows the person everywhere. Capping a member of another
            // org means opening that org's context (cross-org listing is a
            // follow-up, not a picker bug).
            'No members found in this GitHub org. The picker lists this org\'s members only — to cap a member of another GitHub org, open Budget Management in that org.'
          )}
        </div>
      ) : useManualInput || entityOptions.length === 0 ? (
        <div>
          <Input
            label="Entity ID"
            value={entityId}
            onChange={(e) => onEntityIdChange(e.target.value)}
            placeholder={getPlaceholderForEntityType(entityType)}
            disabled={disabled}
            required
            error={error || undefined}
            helperText={
              // Issue #4511: the old copy suggested `user-123 or user email`,
              // neither of which can ever be a valid user key. Name the forms
              // the server actually accepts. #4536: the accepted input forms are
              // identical for both person-scoped kinds — the server resolves them
              // to whichever key that ledger uses — so one hint serves both.
              entityType === EntityType.USER || entityType === EntityType.ROOT_USER
                ? "Enter the user's Cognito sub, their ADP user ID, or their Cognito username (GitHub_<id>). Email is not accepted."
                : entityOptions.length === 0 && !error
                  ? 'No entities found. Enter the ID manually.'
                  : undefined
            }
          />
        </div>
      ) : (
        <Select
          label={isPersonLimit ? 'Person' : 'Entity ID'}
          options={entityOptions}
          value={entityId}
          onChange={(e) => onEntityIdChange(e.target.value)}
          placeholder={isPersonLimit ? 'Select a person...' : 'Select an entity...'}
          disabled={disabled}
          required
        />
      )}

      {/* No manual-entry escape hatch for the person limit — see the branch above. */}
      {!isPersonLimit && !useManualInput && entityOptions.length > 0 && (
        <button
          type="button"
          className="text-sm text-primary-600 hover:text-primary-700 dark:text-primary-400 dark:hover:text-primary-300 underline"
          onClick={() => setUseManualInput(true)}
        >
          Enter ID manually instead
        </button>
      )}

      {useManualInput && entityOptions.length > 0 && (
        <button
          type="button"
          className="text-sm text-primary-600 hover:text-primary-700 dark:text-primary-400 dark:hover:text-primary-300 underline"
          onClick={() => setUseManualInput(false)}
        >
          Select from list instead
        </button>
      )}
    </div>
  );
}

function getPlaceholderForEntityType(entityType: string): string {
  switch (entityType) {
    case EntityType.ORGANIZATION:
      return 'e.g., org-001';
    case EntityType.DEPARTMENT:
      return 'e.g., dept-001 or engineering';
    case EntityType.TEAM:
      return 'e.g., team-001 or platform-team';
    case EntityType.USER:
    case EntityType.ROOT_USER:
      // Issue #4511: a Cognito sub is a UUID. The old `user-123 or
      // user@example.com` hint named two forms that can never be valid keys.
      return 'e.g., 8a41f2c0-1b7d-4e5a-9c33-... or GitHub_20402445';
    default:
      return 'Enter entity ID';
  }
}
