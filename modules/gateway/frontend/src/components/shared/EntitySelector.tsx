/**
 * Entity Selector Component
 *
 * Issue #220: Fix Admin UI Budget/RateLimit CRUD + Organization Page for Org Admins
 * Issue #226: Cognito-backed endpoints as single source of truth (SUPERSEDED — see #4948).
 *
 * A shared component for selecting entity type and entity ID when creating/editing
 * budgets and rate limits.
 *
 * Issue #4948: the org / department / team rungs read the PLATFORM's own tenancy
 * tables, not Cognito groups. Before this they were sourced from unique
 * `custom:org_id` / `custom:department_id` / `custom:team_id` values scraped off
 * Cognito users, which made every platform-natively created org (#4841) and its
 * default department and team invisible to every governance form: an org with no
 * signed-in Cognito members contributes no attribute values to scrape, so it could
 * not be given a budget or a rate limit at all.
 *
 * THE ID NAMESPACES. Enforcement compares the stored config against the caller's
 * token claims with raw string equality and no translation
 * (`_get_entity_hierarchy` / `_check_entity_budget`), so the picker must emit ids
 * in exactly the namespace those claims carry:
 *
 *   organization -> `organizations.id`  (the `custom:org_id` claim)
 *   department   -> `departments.id`    (claim-synced from `team.department_id`)
 *   team         -> `teams.id`          (the `custom:team_id` claim, which is a
 *                                       projection of `users.team_id`, itself the
 *                                       server-maintained pointer at the primary
 *                                       `team_memberships` row)
 *
 * The tenancy routes below return exactly these ids. An id in any other namespace —
 * a Cognito group name, a display name — yields a config that stores cleanly, reads
 * back "capped", and is never matched: the #4511 inert-config class one rung over.
 */

import { useState, useEffect } from 'react';
import { Select } from '@/components/ui/Select';
import { Input } from '@/components/ui/Input';
import {
  getOrgUsers,
  getOrganizations,
  getDepartments,
  getOrgTeams,
} from '@/services/admin';
import { EntityType } from '@/types';
import {
  formatEntityType,
  entityTypeHelpText,
  PERSON_LIMIT_LABEL,
  PERSON_LIMIT_OPTION_VALUE,
  WORKSPACE_NOUN,
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
  /**
   * The org whose partition the config must be WRITTEN to — Issue #4948.
   *
   * This is the anti-trap half of the fix, and it is not optional decoration.
   * Enforcement matches a config on TWO columns, not one:
   *
   *   BudgetConfig.org_id    == context.attributed_org_id   <- the partition
   *   BudgetConfig.entity_id == <the rung's claim>          <- the entity
   *
   * Before this the org option could only ever be the caller's own org, so the
   * partition and the entity id agreed by construction. Offering the full org list
   * breaks that coincidence: a platform admin picking another org while the form
   * still posts to the caller's own `/organizations/{orgId}/budgets` writes
   * `org_id=<caller's org>, entity_id=<picked org>`, and enforcement for a member of
   * the picked org looks in the picked org's partition — so the row matches nothing.
   *
   * A consumer that offers this picker MUST therefore route its write to the org
   * reported here. Callers that do not pass it get no org picker at all (the
   * caller's own org, exactly as before), so a surface that forgets this cannot
   * silently author inert configs — it just loses the cross-org affordance. That is
   * the correct direction for the mistake to fall.
   */
  onScopeOrgChange?: (orgId: string) => void;
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

/**
 * Every tenancy rung this component sources from the platform's own tables — Issue #4948.
 *
 * The person-scoped kinds are deliberately NOT here: they key off the caller's own org
 * and their partition is already correct, so the org picker is not offered for them and
 * widening their scope is out of this issue's scope (#4511/#4536/#4687 own those keys).
 */
const tenancyEntityTypes: string[] = [
  EntityType.ORGANIZATION,
  EntityType.DEPARTMENT,
  EntityType.TEAM,
];

export function EntitySelector({
  orgId,
  entityType,
  entityId,
  onEntityTypeChange,
  onEntityIdChange,
  onScopeOrgChange,
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
  /**
   * Whether this kind is scoped by an organization — Issue #4948.
   *
   * Governs both the org picker's presence and which id the entity list is fetched
   * for. Only offered when the consumer supplied `onScopeOrgChange`: without a
   * consumer that redirects the WRITE to the picked org, a cross-org selection would
   * author a config in the wrong partition (see the prop's docstring).
   */
  const isTenancyScoped = tenancyEntityTypes.includes(entityType);
  const offerOrgPicker = isTenancyScoped && !!onScopeOrgChange;
  /**
   * The org the entity list is drawn from AND the org the config is written to.
   *
   * Defaults to the caller's own org, so a consumer that offers no org picker — and
   * every single-org admin — behaves exactly as before this issue.
   */
  const [scopeOrgId, setScopeOrgId] = useState(orgId);
  const [orgOptions, setOrgOptions] = useState<EntityOption[]>([]);
  // Disclosed rather than silent (the #4936 M4 rule): a picker showing page 1 of a
  // longer list reads as the complete set, so the org someone is looking for being
  // absent reads as "it does not exist" — which is the very bug this issue fixes.
  const [orgsTruncated, setOrgsTruncated] = useState(false);
  const [entityOptions, setEntityOptions] = useState<EntityOption[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [useManualInput, setUseManualInput] = useState(false);
  // Bumped by the person-limit failure notice's Retry button (review fix on
  // #4688): the fetch effect otherwise re-runs only on entityType/orgId change,
  // so the old copy's "try again in a moment" pointed at nothing.
  const [fetchNonce, setFetchNonce] = useState(0);

  // The caller's own org is the default write partition, so a change to it must
  // re-point the scope rather than leave a stale org selected (#4948).
  useEffect(() => {
    setScopeOrgId(orgId);
  }, [orgId]);

  /**
   * The organization list — Issue #4948.
   *
   * `GET /admin/organizations` is already scoped server-side by the caller's
   * authority (`get_accessible_organizations`): a platform admin sees every org, an
   * org admin sees only their own. So this is not a privilege widening — it shows the
   * caller the orgs they already administer, which for an org admin is the same
   * single org the hardcoded option used to name.
   */
  useEffect(() => {
    if (!offerOrgPicker) return;

    let cancelled = false;

    getOrganizations({ pageSize: 100 })
      .then((response) => {
        if (cancelled) return;
        setOrgOptions(
          response.items.map((org) => ({
            // `organizations.id` — the namespace the `custom:org_id` claim carries and
            // therefore the only value enforcement can match. Never the name.
            value: org.id,
            label: org.name ? `${org.name} (${org.id})` : org.id,
          }))
        );
        setOrgsTruncated(response.hasMore);
      })
      .catch(() => {
        // The entity pickers below still work against the caller's own org, so a
        // failed org list is a lost affordance rather than a broken form.
        if (!cancelled) setOrgOptions([]);
      });

    return () => {
      cancelled = true;
    };
  }, [offerOrgPicker]);

  // Fetch entities when entity type or scope org changes
  // Issue #4948: sourced from the platform's tenancy tables, not Cognito groups
  useEffect(() => {
    if (!scopeOrgId) return;

    let cancelled = false;

    async function fetchEntities() {
      setIsLoading(true);
      setError(null);
      setEntityOptions([]);
      setUseManualInput(false);

      try {
        let options: EntityOption[] = [];

        switch (entityType) {
          // Issue #4948: the org being governed is the org SELECTED above, which is
          // also the partition the consumer writes to — so the entity id and the
          // partition agree, which is the whole correctness property here. When no
          // org picker is offered this is the caller's own org, exactly as before.
          case EntityType.ORGANIZATION:
            options = [
              {
                value: scopeOrgId,
                label:
                  orgOptions.find((org) => org.value === scopeOrgId)?.label ??
                  `Current Organization (${scopeOrgId})`,
              },
            ];
            break;

          case EntityType.DEPARTMENT: {
            // `departments.id` — the namespace `custom:department_id` is synced from.
            const deptResponse = await getDepartments(scopeOrgId, { pageSize: 100 });
            if (cancelled) return;
            options = deptResponse.items.map((dept) => ({
              value: dept.id,
              label: dept.name ? `${dept.name} (${dept.id})` : dept.id,
            }));
            break;
          }

          case EntityType.TEAM: {
            // T1's ORG-WIDE team route (#4840 / PR #4917), not the department-scoped
            // `getTeams`: a budget may be set on any team in the org, and a
            // department-scoped list would silently hide every team outside whichever
            // department the admin happened to be looking at.
            //
            // `teams.id` is the namespace `custom:team_id` carries — that claim is a
            // projection of `users.team_id`, which the server keeps pointed at the
            // person's primary `team_memberships` row.
            const teamsResponse = await getOrgTeams(scopeOrgId, { pageSize: 100 });
            if (cancelled) return;
            options = teamsResponse.items.map((team) => ({
              value: team.id,
              label: team.name ? `${team.name} (${team.id})` : team.id,
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
    // `scopeOrgId` drives the tenancy rungs (#4948); `orgId` still drives the
    // person-scoped ones, which stay on the caller's own org. `orgOptions` is here so
    // the organization option picks up its friendly name once the list resolves.
  }, [entityType, orgId, scopeOrgId, orgOptions, fetchNonce]);

  return (
    <div className="space-y-4">
      <Select
        label="Entity Type"
        options={entityTypeOptions}
        value={entityType}
        onChange={(e) => {
          const nextType = e.target.value;
          // Person options come from orgId. Reset the write partition along
          // with the picker when leaving a department/team in another org.
          if (!tenancyEntityTypes.includes(nextType)) {
            setScopeOrgId(orgId);
            onScopeOrgChange?.(orgId);
          }
          onEntityTypeChange(nextType);
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

      {/* Issue #4948: the organization the config is authored FOR and written INTO.
          Rendered above the entity picker because it narrows it — and only for the
          tenancy-scoped kinds, whose ids live inside one org. */}
      {offerOrgPicker && (
        <div>
          <Select
            label={WORKSPACE_NOUN}
            options={orgOptions.map((org) => ({ value: org.value, label: org.label }))}
            value={scopeOrgId}
            onChange={(e) => {
              const nextOrgId = e.target.value;
              setScopeOrgId(nextOrgId);
              // Any previously picked department or team belongs to the PREVIOUS org, and
              // an id from another org is exactly the cross-tenant config this cascade
              // exists to prevent.
              onEntityIdChange('');
              // Redirect the write, not just the list. See `onScopeOrgChange`.
              onScopeOrgChange?.(nextOrgId);
            }}
            disabled={disabled || orgOptions.length === 0}
            required
            helperText={
              entityType === EntityType.ORGANIZATION
                ? undefined
                : `Departments and teams are listed for this ${WORKSPACE_NOUN.toLowerCase()}.`
            }
          />
          {orgsTruncated && (
            // Disclosed, not silent (#4936 M4): an absent org must not read as
            // "does not exist" — that misreading IS this issue.
            <p
              className="mt-1 text-xs text-amber-600 dark:text-amber-400"
              data-testid="orgs-truncated-warning"
            >
              Not every {WORKSPACE_NOUN.toLowerCase()} is listed — there are more than one
              page shows.
            </p>
          )}
        </div>
      )}

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
