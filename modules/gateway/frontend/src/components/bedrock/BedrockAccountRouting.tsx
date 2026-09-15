/**
 * "Bedrock Account Routing" — Issue #4745 (#4692 · R4), §6.3 and the mockup at
 * `docs/mockups/4692-bedrock-account-routing-admin.html`.
 *
 * Budget Management answers how much a principal may spend. This panel answers the
 * adjacent question: **whose AWS account is billed for it.** Ruling 4b enumerates the
 * three elements, and each one exists to prevent a specific misreading:
 *
 * 1. **The rules table names the rung.** Showing a person's effective destination
 *    without saying which rule produced it invites the reader to "fix" the wrong row —
 *    the #4511 discipline applied to a UI, and the labelled-source requirement #4691
 *    reached independently for budget limits.
 *
 * 2. **The destination dropdown is scoped to the org being mapped**, and lists only
 *    verified, routing-capable rows. Per §4.2 requirement 1 that is a *usability*
 *    feature; the API refuses an out-of-scope or unusable destination on save either
 *    way. The two are kept in step so the panel never offers a choice the server
 *    rejects — but the panel is not the control, and the code says so where it matters.
 *
 * 3. **"Register new destination"** renders `ConnectAwsForm`, the same component the
 *    credentials page uses (§6.6). Not a copy: §6.6 names a second quick-create flow as
 *    the failure mode to avoid.
 *
 * Three properties of the routing ladder are load-bearing in the rendering:
 *
 * - **Fail-closed** (ruling 1, §2.5). A mapped account that cannot serve a call fails
 *   the call; it does not fall back to the platform account. The banner says this
 *   unprompted, because ruling 1's behaviour is surprising if undisclosed, and because
 *   it is why an unusable destination is worth an admin's attention now rather than
 *   after somebody is paged.
 *
 * - **An unusable destination is NO MATCH, not a failed rule** (§4.4). The resolver
 *   skips it and walks on. So a rule pointing at a broken destination is shown with a
 *   warning rather than as an error, and re-verifying a destination never deletes the
 *   rules aimed at it.
 *
 * - **Admin wins at the user rung** (§1.4 SETTLED), *and the UI must say so*. A
 *   self-selected row is greyed and marked "managed by user"; an admin rule over one
 *   reports `overrides_self_selection`. Showing a person's own stale pick as active
 *   would be the #4511 inert-config defect one layer up.
 *
 * **Why there IS a complete rules table here** — unlike `DefaultPersonLimits`, which has
 * to disclaim completeness because its API is per-scope only. `GET /mappings` returns
 * every authored row platform-wide, so this table is the real inventory and can be read
 * as one.
 *
 * **The org and team pickers source from the tenancy model** (#4947). Teams come from
 * `GET /admin/organizations/{org}/teams` — the `teams` table — and the value submitted is
 * a `teams.id`, because that is the namespace the request-path resolver compares against
 * (`custom:team_id` ← `users.team_id` ← the member's primary `teams.id`). The panel
 * previously read the Cognito-derived group list, which knew nothing about a team created
 * in the tenancy admin console, so the team rung could not be authored for any org built
 * that way at all.
 *
 * The panel assumes it is mounted only for a platform admin; `BudgetManagement` does the
 * gating. That gate is an affordance — every route enforces `require_platform_admin`
 * server-side, which is the actual boundary.
 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import { Alert, Button, Card, Input, Modal, Select } from '@/components/ui';
import { LinkAwsConnectionModal } from './LinkAwsConnectionModal';
import { ConnectAwsForm } from '@/components/aws/ConnectAwsForm';
import { useToast } from '@/contexts/ToastContext';
import { useDebounce } from '@/hooks/useDebounce';
import { getOrganizations, getOrgTeams, listPlatformUsers, type PlatformUser } from '@/services/admin';
import type { Team } from '@/types';
import {
  deleteMapping,
  getEffectiveMapping,
  listDestinations,
  listMappings,
  registerDestination,
  setMapping,
  verifyDestination,
  unlinkAwsConnection,
} from '@/services/bedrockRouting';
import { describeRoutingReason } from '@/types/bedrockRouting';
import type {
  DestinationSummary,
  EffectiveMappingResponse,
  MappingScope,
  MappingScopeType,
  MappingSummary,
} from '@/types/bedrockRouting';

/** The mockup's scope filter. `platform` is not here because no such row is authored. */
const SCOPE_FILTERS: Array<{ value: string; label: string }> = [
  { value: 'all', label: 'All scopes' },
  { value: 'org', label: 'Org' },
  { value: 'team', label: 'Team' },
  { value: 'user', label: 'User' },
];

/** `…4821`, the mockup's account rendering. Full ids appear in the destinations table. */
function shortAccount(accountId: string): string {
  return `…${accountId.slice(-4)}`;
}

/**
 * Every team in one org, keyed by `teams.id`, plus whether that read was COMPLETE.
 *
 * `complete` is what licenses the "team not found" flag: a truncated page or a failed
 * request means "we could not ask", and rendering that as "no such team" would send an
 * admin to re-create a team that exists — the same distinction `PersonPicker` draws for
 * people, applied to the rung above.
 */
interface OrgTeamIndex {
  byId: Map<string, Team>;
  complete: boolean;
}

/**
 * The scope in the "APPLIES TO" column, from the row's own ids.
 *
 * Built from the response rather than from whatever the form held, so a row always
 * describes the scope the server actually stored.
 *
 * **The team rung names the team, and says when it cannot** — Issue #4947. `scope_id_team`
 * is a `teams.id`; a row authored before this panel read the tenancy model holds a Cognito
 * *group name* in that column instead, which the resolver compares against `users.team_id`
 * and never matches. Such a rule reads back as configured routing and governs nobody — the
 * #4511 inert-config class — so it is rendered with its raw id and flagged rather than
 * hidden or silently prettified. The flag is withheld unless the org's team list was read
 * in full, per `OrgTeamIndex.complete`.
 */
function describeMappingScope(mapping: MappingSummary, teamIndex?: OrgTeamIndex): { text: string; unresolvedTeam: boolean } {
  if (mapping.scope_type === 'org') return { text: mapping.scope_id_org ?? '—', unresolvedTeam: false };
  if (mapping.scope_type === 'team') {
    const teamId = mapping.scope_id_team;
    const team = teamId ? teamIndex?.byId.get(teamId) : undefined;
    return {
      text: `${mapping.scope_id_org ?? '?'} / ${team?.name ?? teamId ?? '?'}`,
      unresolvedTeam: !!teamId && !team && !!teamIndex?.complete,
    };
  }
  return { text: mapping.scope_id_user ?? '—', unresolvedTeam: false };
}

/**
 * The human-readable half of a rejection.
 *
 * `apiClient` throws the parsed response body itself, so a refusal from this API arrives
 * as `{detail: {reason, message}}` — the server's own prose, which is preferred because
 * it names the specific destination or scope that was refused. A `404` sends `detail` as
 * a bare string, and a transport failure has only `message`; both are read rather than
 * collapsed into the fallback, since "Could not save" tells an operator nothing they can
 * act on.
 *
 * A rejection here is a **normal outcome to display**, not an exception to swallow: the
 * save runs a real assume-role probe and refusing is the #4511 discipline working.
 */
function rejectionMessage(err: unknown, fallback: string): string {
  const detail = (err as { detail?: unknown })?.detail;
  if (typeof detail === 'string' && detail) return detail;
  const structured = detail as { reason?: string; message?: string } | undefined;
  if (structured?.message) return structured.message;
  const described = describeRoutingReason(structured?.reason);
  if (described) return described;
  const message = (err as { message?: string })?.message;
  return message || fallback;
}

/**
 * How a person is named in the picker — Issue #4827.
 *
 * The **GitHub username leads when one is linked**, because that is how operators
 * recognise people (operator requirement, 2026-09-08). The email follows so two
 * people with similar logins stay distinguishable, and the org is a display hint —
 * this picker is platform-wide, so without it an operator has no way to tell two
 * tenants' same-named members apart.
 *
 * A member with no GitHub identity is labelled as such rather than shown with a blank
 * column. That state is permanent and legitimate (email/invite onboarding), and saying
 * "no GitHub linked" out loud is what stops an operator reading a missing login as a
 * loading failure and picking the wrong row.
 *
 * Never the id. The whole defect being fixed is that ids are unrecognisable — putting
 * one back in the label would reintroduce it in a dropdown instead of an input.
 */
function describePerson(person: PlatformUser): string {
  const who = person.email || person.name || person.id;
  return person.githubUsername ? `${person.githubUsername} — ${who} (${person.orgId})` : `${who} (${person.orgId}, no GitHub linked)`;
}

/**
 * The person picker — Issue #4827.
 *
 * Replaces a free-text field asking for an internal `users.id`. The server always
 * refused a wrong id (`require_scope_exists`, a 422 naming the mistake), so nothing was
 * ever mis-routed; what was missing was any way for an admin to produce a RIGHT id, and
 * a control an operator cannot satisfy is indistinguishable from a broken one.
 *
 * **Platform-wide, not org-scoped** (operator requirement, 2026-09-08). A platform admin
 * may pin any user in any org, so scoping the list to one org would hide exactly the
 * people that authority covers. That is also why there is no org selector above it: the
 * user rung's mapping row carries no org, and asking for one would imply the choice
 * narrows the rule when it does not.
 *
 * **Search is server-side and debounced, with the first page preloaded.** Fetching the
 * whole member table on mount is the failure mode the endpoint's pagination exists to
 * prevent; a picker that silently shows only page 1 of a large platform is the other
 * (#4688's lesson, where a single 100-row page made members #101+ un-cappable with
 * nothing on screen saying why). Here the truncation is *stated* — see the count note —
 * because search, not a bounded page walk, is the way through a platform-sized roster.
 *
 * **A load failure is surfaced, never rendered as "nobody matches".** An empty picker
 * that means "we could not ask" reads as "this person does not exist", which sends an
 * admin to create a user that already exists.
 *
 * The selected value is always the canonical `users.id` — the column the resolver and
 * the server-side check compare against (#4647). The picker keeps the server's 422 as
 * the real guarantee; it is a usability feature, not the control.
 *
 * Exported for the Members panel's add-member modal (#4847, PR #4936 review M2): the
 * mockup names this exact picker ("Same searchable picker as Bedrock Account
 * Routing"), and a second GitHub-ID-first person picker would be the fork the story
 * forbids. Note the roster read (`listPlatformUsers`) is platform-admin-only, so a
 * caller must gate the affordance accordingly.
 */
export function PersonPicker({
  label,
  namePrefix,
  value,
  onChange,
  helperText,
}: {
  label: string;
  namePrefix: string;
  value: string;
  /**
   * The chosen `users.id`, plus the roster ROW it came from when the picker has it.
   *
   * The second argument exists because the row carries `orgId`, and a caller
   * assigning somebody to an org-scoped resource has to know whether the person is
   * in that org before it fires a write the server can only refuse (#4943: the
   * members panel's add-member modal). Optional and second so every existing caller
   * — which only needs the id — keeps working unchanged. `null` when the selection
   * is cleared, or when the person was chosen and then searched out of the loaded
   * page; a caller must treat that as "not known", never as "not in this org".
   */
  onChange: (userId: string, person: PlatformUser | null) => void;
  helperText?: string;
}) {
  const [search, setSearch] = useState('');
  const [people, setPeople] = useState<PlatformUser[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const debouncedSearch = useDebounce(search, 300);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    listPlatformUsers({ q: debouncedSearch, pageSize: 50 })
      .then((res) => {
        if (cancelled) return;
        setPeople(res.items);
        setTotal(res.total);
        setError(null);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setPeople([]);
        setTotal(0);
        setError(rejectionMessage(err, 'The list of people could not be loaded.'));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [debouncedSearch]);

  /**
   * The chosen person, remembered so a later search cannot drop them off the list.
   *
   * Without this, typing a narrower search after choosing somebody removes the
   * selection from the `<select>`: the browser reports an empty value, the save button
   * disables itself, and nothing on screen says why. Held as the person rather than
   * re-derived from `people`, because the whole point is that they may no longer be in
   * it. Cleared when `value` is cleared, so a re-opened modal starts empty.
   */
  const [chosen, setChosen] = useState<PlatformUser | null>(null);
  useEffect(() => {
    if (!value) setChosen(null);
  }, [value]);

  const options = useMemo(() => {
    const rows = chosen && !people.some((p) => p.id === chosen.id) ? [chosen, ...people] : people;
    return rows.map((p) => ({ value: p.id, label: describePerson(p) }));
  }, [people, chosen]);

  return (
    <div className="space-y-2">
      <Input
        label={`Find ${label.toLowerCase()}`}
        name={`${namePrefix}-search`}
        value={search}
        onChange={(e) => setSearch(e.target.value)}
        placeholder="Search by GitHub username, name, or email"
        data-testid={`${namePrefix}-search`}
      />

      <Select
        label={label}
        name={namePrefix}
        value={value}
        onChange={(e) => {
          const person = people.find((p) => p.id === e.target.value) ?? null;
          onChange(e.target.value, person);
          setChosen(person);
        }}
        placeholder={loading ? 'Loading people…' : options.length ? 'Select a person' : 'No matching person'}
        disabled={!options.length}
        options={options}
        data-testid={namePrefix}
        helperText={helperText}
      />

      {error && (
        // Stated rather than shown as an empty list: "we could not ask" must never
        // read as "no such person", which is what gets a duplicate user created.
        <p className="text-xs text-red-700 dark:text-red-400" data-testid={`${namePrefix}-error`}>
          {error} This is not a statement that the person does not exist.
        </p>
      )}

      {!error && total > options.length && (
        // The cap is disclosed, not silent. A picker that shows 50 of 900 and says
        // nothing is read as the complete roster.
        <p className="text-xs text-gray-500 dark:text-gray-400" data-testid={`${namePrefix}-truncated`}>
          Showing {options.length} of {total} people. Narrow the search to find someone who is not listed.
        </p>
      )}
    </div>
  );
}

/**
 * A destination's verification state, as the mockup's LAST VERIFIED column.
 *
 * Three states, not two. "Never verified" is distinct from "failed": one is a
 * registration waiting for its CloudFormation stack, the other is a destination that has
 * stopped working. Collapsing them would tell an admin to debug IAM for an account whose
 * role does not exist yet.
 */
function VerificationState({ destination }: { destination: DestinationSummary }) {
  if (destination.usable_for_routing) {
    return (
      <span className="text-green-700 dark:text-green-400" data-testid={`routing-dest-ok-${destination.id}`}>
        ● Verified
      </span>
    );
  }
  if (destination.reason) {
    return (
      <span className="text-red-700 dark:text-red-400 font-medium" data-testid={`routing-dest-failed-${destination.id}`}>
        ✗ FAILED — {describeRoutingReason(destination.reason)}
      </span>
    );
  }
  return (
    <span className="text-gray-500 dark:text-gray-400" data-testid={`routing-dest-unverified-${destination.id}`}>
      Not verified yet
    </span>
  );
}

/**
 * The add / edit rule modal.
 *
 * The destination options are the caller's, already filtered — the modal does not fetch,
 * so that the "only in-scope, verified, routing-capable destinations are offered" rule
 * lives in exactly one place.
 *
 * **A failed save keeps the modal open.** The save runs a real assume-role probe and can
 * legitimately refuse (ruling 4a); closing on failure would leave the operator believing
 * traffic had been rerouted when nothing was stored — the inverse of the #4511 defect the
 * probe exists to prevent.
 */
function AddRuleModal({
  isOpen,
  onClose,
  onSaved,
  orgs,
  orgsTruncated,
  destinations,
  initialScope,
}: {
  isOpen: boolean;
  onClose: () => void;
  onSaved: (message: string) => void;
  orgs: Array<{ id: string; name: string }>;
  orgsTruncated: boolean;
  destinations: DestinationSummary[];
  initialScope?: MappingScope;
}) {
  const toast = useToast();
  const [scopeType, setScopeType] = useState<MappingScopeType>('team');
  const [orgId, setOrgId] = useState('');
  const [teamId, setTeamId] = useState('');
  const [userId, setUserId] = useState('');
  const [teams, setTeams] = useState<Team[]>([]);
  const [teamsTruncated, setTeamsTruncated] = useState(false);
  const [teamsError, setTeamsError] = useState<string | null>(null);
  const [destinationId, setDestinationId] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  // Re-seed on open: a scope left over from the previously edited rule would be an
  // authoring mistake pre-filled for the operator.
  useEffect(() => {
    if (!isOpen) return;
    setScopeType(initialScope?.scope_type ?? 'team');
    setOrgId(initialScope?.org ?? '');
    setTeamId(initialScope?.team ?? '');
    setUserId(initialScope?.user ?? '');
    setDestinationId('');
    setError(null);
  }, [isOpen, initialScope]);

  /**
   * The org's teams, from the TENANCY MODEL — Issue #4947.
   *
   * `getOrgTeams` is `GET /admin/organizations/{org}/teams` (#4840), the org-wide list
   * over the `teams` table. It replaces `getCognitoTeams`, which derived a list by
   * scanning the Cognito user pool for distinct `custom:team_id` attribute *values* and
   * therefore knows nothing about a team created in the tenancy admin console: for an org
   * built that way the picker came back empty and the team rung was unauthorable.
   *
   * **The option value is `teams.id`, which is the namespace the resolver matches.**
   * `BedrockRoutingResolver` compares `scope_id_team` to `TokenContext.team_id` — the
   * `custom:team_id` claim, which is projected from `users.team_id`, which
   * `admin/team_memberships.py` keeps pointed at the member's primary `teams.id`. Storing
   * anything else (a name, a Cognito group) yields a rule that reads back as configured
   * routing and fires for nobody. `require_scope_exists` refuses one server-side; this
   * picker is why an admin does not hit that refusal.
   *
   * Teams are per-org, so the list reloads with the org and the previously picked team is
   * cleared — a team id from another org is exactly the cross-tenant mistake the two-id
   * scope form exists to prevent (#4344).
   *
   * A failed read is held as an ERROR, never as an empty list: "we could not ask" reading
   * as "this org has no teams" is what sends an admin to create a team that exists.
   */
  useEffect(() => {
    if (scopeType !== 'team' || !orgId) {
      setTeams([]);
      setTeamsTruncated(false);
      setTeamsError(null);
      return;
    }
    let cancelled = false;
    getOrgTeams(orgId, { pageSize: 100 })
      .then((res) => {
        if (cancelled) return;
        setTeams(res.items);
        setTeamsTruncated(res.hasMore);
        setTeamsError(null);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setTeams([]);
        setTeamsTruncated(false);
        setTeamsError(rejectionMessage(err, 'The list of teams could not be loaded.'));
      });
    return () => {
      cancelled = true;
    };
  }, [scopeType, orgId]);

  /**
   * Destinations this scope may legitimately name.
   *
   * Usable rows only, and for org/team/user rungs, rows linked to the scope's org or
   * registered platform-wide. This mirrors the server's §4.2 requirement-1 check; the
   * server is what enforces it.
   */
  const options = useMemo(() => {
    const scopeOrg = scopeType === 'user' ? null : orgId;
    return destinations.filter((d) => {
      if (!d.usable_for_routing) return false;
      if (!scopeOrg) return true;
      return d.owner_org_id === scopeOrg || d.owner_org_id === null;
    });
  }, [destinations, scopeType, orgId]);

  const scope: MappingScope = {
    scope_type: scopeType,
    org: scopeType === 'user' ? undefined : orgId,
    team: scopeType === 'team' ? teamId : undefined,
    user: scopeType === 'user' ? userId : undefined,
  };

  // No `.trim()` on the user id any more: since #4827 it is a canonical id chosen from
  // a server-sourced list, not something an operator typed, so there is no whitespace
  // to defend against and trimming would only hide a real id-shape bug.
  const ready = !!destinationId && ((scopeType === 'org' && !!orgId) || (scopeType === 'team' && !!orgId && !!teamId) || (scopeType === 'user' && !!userId));

  const handleSave = async () => {
    setSaving(true);
    setError(null);
    try {
      await setMapping(scope, destinationId);
      onSaved(
        scopeType === 'user'
          ? 'Routing rule saved. This overrides whatever the person selected on their own credentials page.'
          : 'Routing rule saved.'
      );
      onClose();
    } catch (err: unknown) {
      const message = rejectionMessage(err, 'Could not save the routing rule.');
      // Kept open on purpose — see the component docstring.
      setError(message);
      toast.error(message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal isOpen={isOpen} onClose={onClose} title="Add routing rule" size="md">
      <div className="space-y-4">
        <div>
          <span className="text-sm font-medium text-gray-700 dark:text-gray-300">Scope</span>
          <div className="flex gap-4 mt-1 text-sm">
            {(['org', 'team', 'user'] as MappingScopeType[]).map((value) => (
              <label key={value} className="flex items-center gap-1 capitalize">
                <input
                  type="radio"
                  name="routing-scope-type"
                  value={value}
                  checked={scopeType === value}
                  onChange={() => setScopeType(value)}
                  data-testid={`routing-scope-${value}`}
                />
                {value}
              </label>
            ))}
          </div>
        </div>

        {scopeType !== 'user' && (
          <div>
            <Select
              label="Organization"
              name="routing-rule-org"
              value={orgId}
              onChange={(e) => {
                setOrgId(e.target.value);
                setTeamId('');
                // The in-scope destination set changes with the org, so a destination
                // picked for the previous one must not survive the switch.
                setDestinationId('');
              }}
              placeholder="Select an organization"
              options={orgs.map((o) => ({ value: o.id, label: o.name || o.id }))}
            />
            {orgsTruncated && (
              // The org read is a single page (#4914's caveat, #4936's M4 pattern). An
              // admin whose org is on page 2 must be told the list is cut rather than
              // conclude their org was never created.
              <p className="mt-1 text-xs text-amber-600 dark:text-amber-400" data-testid="routing-orgs-truncated">
                Not every organization is listed — this platform has more organizations than one page shows.
              </p>
            )}
          </div>
        )}

        {scopeType === 'team' && (
          <div>
            <Select
              label="Team"
              name="routing-rule-team"
              value={teamId}
              onChange={(e) => setTeamId(e.target.value)}
              placeholder={
                !orgId
                  ? 'Select an organization first'
                  : teamsError
                    ? 'The teams could not be loaded'
                    : teams.length
                      ? 'Select a team'
                      : 'This organization has no teams'
              }
              disabled={!orgId || !teams.length}
              // The value is `teams.id` — see the load effect above. The name is the
              // label only; a rule authored against a name governs nobody.
              options={teams.map((t) => ({ value: t.id, label: t.name || t.id }))}
            />
            {teamsError && (
              // Stated, not shown as an empty list, for the same reason `PersonPicker`
              // states its own failure: an absence of options must not read as an
              // absence of teams.
              <p className="mt-1 text-xs text-red-700 dark:text-red-400" data-testid="routing-rule-teams-error">
                {teamsError} This is not a statement that the organization has no teams.
              </p>
            )}
            {teamsTruncated && (
              <p className="mt-1 text-xs text-amber-600 dark:text-amber-400" data-testid="routing-rule-teams-truncated">
                Not every team is listed — this organization has more teams than one page shows.
              </p>
            )}
            {!teamsError && (
              // The tenancy list can offer a team nobody is on yet, whereas the old
              // Cognito-derived list could not by construction. The server refuses such a
              // rule (it would govern nobody), so the condition is disclosed here rather
              // than met as a surprise refusal on save.
              <p className="mt-1 text-xs text-gray-500 dark:text-gray-400" data-testid="routing-rule-team-empty-note">
                A team with no members yet cannot be routed — the rule would govern nobody, and saving it is refused.
              </p>
            )}
          </div>
        )}

        {scopeType === 'user' && (
          <PersonPicker
            label="Person"
            namePrefix="routing-rule-user"
            value={userId}
            onChange={setUserId}
            helperText="An admin rule here takes precedence over the person's own selection."
          />
        )}

        <Select
          label="Destination"
          name="routing-rule-destination"
          value={destinationId}
          onChange={(e) => setDestinationId(e.target.value)}
          placeholder={options.length ? 'Select a destination' : 'No usable destination available'}
          disabled={!options.length}
          options={options.map((d) => ({
            value: d.id,
            label: `${d.label} (${shortAccount(d.account_id)}) — verified`,
          }))}
        />
        <p className="text-xs text-gray-500 dark:text-gray-400" data-testid="routing-destination-scope-note">
          {scopeType === 'user'
            ? 'Only verified, routing-capable destinations are listed.'
            : 'Only verified destinations linked to the selected organization (or registered platform-wide) are listed.'}
        </p>

        {/* Ruling 1's behaviour, stated where the decision is made rather than only in
            the banner: this is the moment an admin can still choose differently. */}
        <Alert variant="warning" title="These calls will fail if the destination cannot serve them">
          <p data-testid="routing-fail-closed-warning">
            If a model is not enabled in the destination account, or its role stops being assumable, the affected calls fail with a clear error. They
            do not fall back to the platform account.
          </p>
        </Alert>

        {error && (
          <Alert variant="error" title="The rule was not saved">
            <p data-testid="routing-rule-error">{error}</p>
          </Alert>
        )}

        <p className="text-xs text-gray-500 dark:text-gray-400">
          Saving runs a real test assume-role against the destination. A rule that cannot be assumed, or whose role cannot invoke Bedrock, is rejected
          and nothing is stored.
        </p>

        <div className="flex justify-end gap-2">
          <Button variant="outline" onClick={onClose} disabled={saving}>
            Cancel
          </Button>
          <Button onClick={handleSave} disabled={saving || !ready} data-testid="routing-rule-save">
            {saving ? 'Testing and saving…' : 'Save rule'}
          </Button>
        </div>
      </div>
    </Modal>
  );
}

/**
 * The remove confirmation.
 *
 * Its whole job is the sentence about the ladder. An admin removing a team rule is
 * likely to expect that team's traffic to stop working; what actually happens is they
 * fall back to their org's rule, or the platform account. Saying nothing would leave the
 * more alarming reading in place.
 */
function RemoveRuleModal({
  mapping,
  teamIndex,
  onClose,
  onRemoved,
}: {
  mapping: MappingSummary | null;
  teamIndex?: OrgTeamIndex;
  onClose: () => void;
  onRemoved: (message: string) => void;
}) {
  const toast = useToast();
  const [removing, setRemoving] = useState(false);

  const handleRemove = async () => {
    if (!mapping) return;
    setRemoving(true);
    try {
      await deleteMapping({
        scope_type: mapping.scope_type,
        org: mapping.scope_id_org ?? undefined,
        team: mapping.scope_id_team ?? undefined,
        user: mapping.scope_id_user ?? undefined,
      });
      onRemoved('Routing rule removed.');
      onClose();
    } catch (err: unknown) {
      toast.error(rejectionMessage(err, 'Could not remove the routing rule.'));
    } finally {
      setRemoving(false);
    }
  };

  return (
    <Modal isOpen={!!mapping} onClose={onClose} title="Remove this routing rule?" size="md">
      <div className="space-y-4">
        {mapping && (
          <p className="text-sm text-gray-700 dark:text-gray-300" data-testid="routing-remove-scope">
            {mapping.scope_type.toUpperCase()} · {describeMappingScope(mapping, teamIndex).text} → {mapping.destination_label} (
            {shortAccount(mapping.destination_account_id)})
          </p>
        )}

        <Alert variant="warning" title="Their calls will not stop working">
          <p data-testid="routing-remove-ladder">
            Removing this rule does not leave these principals unroutable — they fall back to the next rule that applies (team, then organization,
            then the platform account). Their Bedrock spend moves to whichever account that is.
          </p>
        </Alert>

        <div className="flex justify-end gap-2">
          <Button variant="outline" onClick={onClose} disabled={removing}>
            Cancel
          </Button>
          <Button variant="danger" onClick={handleRemove} disabled={removing} data-testid="routing-remove-confirm">
            {removing ? 'Removing…' : 'Remove rule'}
          </Button>
        </div>
      </div>
    </Modal>
  );
}

/**
 * "Register new destination account" — §6.6, rendering the shared `ConnectAwsForm`.
 *
 * The extra field is "Link to org", which the mockup has and which the server requires:
 * every destination this endpoint mints has an owning tenant, so the cross-tenant scope
 * check stays total.
 *
 * **The mockup's "Role name" input is deliberately absent.** The v2 template names the
 * role `ADP-Agent-${Nickname}` and declares no role-name parameter, so a field here
 * would be a control that appears to configure something and does not — the #4511 shape.
 * The nickname is what names the role.
 */
function RegisterDestinationModal({
  isOpen,
  onClose,
  onRegistered,
  orgs,
}: {
  isOpen: boolean;
  onClose: () => void;
  onRegistered: (message: string) => void;
  orgs: Array<{ id: string; name: string }>;
}) {
  const [linkOrgId, setLinkOrgId] = useState('');

  useEffect(() => {
    if (isOpen) setLinkOrgId('');
  }, [isOpen]);

  return (
    <Modal isOpen={isOpen} onClose={onClose} title="Register new destination account" size="md">
      <div className="space-y-4">
        <p className="text-sm text-gray-600 dark:text-gray-400">
          The same CloudFormation quick-create flow as <em>Connect AWS Account</em>, saved into the platform routing registry rather than your
          personal credentials.
        </p>

        <ConnectAwsForm
          testIdPrefix="routing-register"
          extraFieldsValid={!!linkOrgId}
          infoText="Launch opens the AWS Console in the destination account; the template creates the IAM role and its trust policy. The destination is not selectable in a rule until the platform has verified it can assume that role."
          onLaunch={async ({ nickname, accountId }) => {
            const result = await registerDestination({
              source: 'new_account',
              account_id: accountId,
              label: nickname,
              link_to_org_id: linkOrgId,
            });
            return { launch_url: result.launch_url, handle: result.destination.id };
          }}
          onVerify={async (destinationId) => {
            const result = await verifyDestination(destinationId);
            return { verified: result.verified, reason: describeRoutingReason(result.reason) };
          }}
          onVerified={() => {
            onRegistered('Destination registered and verified. It can now be selected in a rule.');
            onClose();
          }}
        >
          <Select
            label="Link to organization"
            name="routing-register-org"
            value={linkOrgId}
            onChange={(e) => setLinkOrgId(e.target.value)}
            placeholder="Select an organization"
            options={orgs.map((o) => ({ value: o.id, label: o.name || o.id }))}
            helperText="Rules for this organization's teams and users may route here."
          />
        </ConnectAwsForm>
      </div>
    </Modal>
  );
}

/**
 * The effective-mapping lookup — "check who serves a person".
 *
 * Reports the rung and, when an admin has overridden somebody's own selection, says so.
 * A failed lookup is left as an error rather than rendered as "platform": a wrong id
 * that resolved confidently would tell an admin this person has no rule when they may
 * have one.
 */
function EffectiveLookup() {
  const [userId, setUserId] = useState('');
  const [result, setResult] = useState<EffectiveMappingResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const handleLookup = async () => {
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      setResult(await getEffectiveMapping(userId));
    } catch (err: unknown) {
      setError(rejectionMessage(err, 'Could not look that person up.'));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="mt-4 border-t border-gray-100 dark:border-gray-800 pt-4">
      <div className="max-w-md">
        {/* The same picker as the rule form, for the same reason (#4827): this field
            took a raw `users.id` too, and an operator who cannot name a person cannot
            check who serves them either. */}
        <PersonPicker label="Check who serves a person" namePrefix="routing-effective-user" value={userId} onChange={setUserId} />
      </div>
      <Button
        variant="secondary"
        size="sm"
        className="mt-2"
        onClick={handleLookup}
        disabled={loading || !userId}
        data-testid="routing-effective-lookup"
      >
        {loading ? 'Looking up…' : 'Look up'}
      </Button>

      {error && (
        <p className="text-sm text-red-700 dark:text-red-400 mt-2" data-testid="routing-effective-error">
          {error}
        </p>
      )}

      {result && (
        <div className="text-sm text-gray-700 dark:text-gray-300 mt-2 space-y-1" data-testid="routing-effective-result">
          {result.rung === 'platform' ? (
            <p>
              → <strong>{result.user_id}</strong> is served by the <strong>platform account</strong> — no rule applies to them.
            </p>
          ) : (
            <p>
              → <strong>{result.user_id}</strong> → <span className="font-mono text-xs">{result.destination_label}</span> (
              {result.account_id ? shortAccount(result.account_id) : '—'}), from the <strong>{result.rung}</strong> rule
              {result.source === 'self' ? ', which they selected themselves on their credentials page' : ''}.
            </p>
          )}

          {/* §1.4's display requirement, stated rather than implied. */}
          {result.overrides_self_selection && (
            <p className="text-amber-700 dark:text-amber-400" data-testid="routing-effective-override">
              A platform admin has pinned this person, which takes precedence over their own selection on the credentials page.
            </p>
          )}

          {result.shadowed_rung && (
            <p className="text-gray-500 dark:text-gray-400" data-testid="routing-effective-shadowed">
              If that rule were removed they would be served by{' '}
              {result.shadowed_rung === 'platform'
                ? 'the platform account'
                : `${result.shadowed_account_id ? shortAccount(result.shadowed_account_id) : 'another account'} via the ${result.shadowed_rung} rule`}
              .
            </p>
          )}
        </div>
      )}
    </div>
  );
}

export function BedrockAccountRouting() {
  const toast = useToast();
  const [mappings, setMappings] = useState<MappingSummary[] | null>(null);
  const [mappingsError, setMappingsError] = useState<string | null>(null);
  const [destinations, setDestinations] = useState<DestinationSummary[] | null>(null);
  const [destinationsError, setDestinationsError] = useState<string | null>(null);
  const [orgs, setOrgs] = useState<Array<{ id: string; name: string }>>([]);
  /** True when the platform has more orgs than the single page below returned. */
  const [orgsTruncated, setOrgsTruncated] = useState(false);
  /** `teams.id` → team, per org, for the rules table's APPLIES TO column (#4947). */
  const [teamIndexes, setTeamIndexes] = useState<Map<string, OrgTeamIndex>>(new Map());
  const [scopeFilter, setScopeFilter] = useState('all');
  const [showAddRule, setShowAddRule] = useState(false);
  const [showRegister, setShowRegister] = useState(false);
  const [showLink, setShowLink] = useState(false);
  const [unlinking, setUnlinking] = useState<DestinationSummary | null>(null);
  const [unlinkBusy, setUnlinkBusy] = useState(false);
  const [unlinkError, setUnlinkError] = useState<string | null>(null);
  const [removing, setRemoving] = useState<MappingSummary | null>(null);
  const [verifyingId, setVerifyingId] = useState<string | null>(null);
  const [confirmation, setConfirmation] = useState<string | null>(null);
  /** Bumped after any write, so both tables re-read — the `DefaultPersonLimits` idiom. */
  const [reloadToken, setReloadToken] = useState(0);

  useEffect(() => {
    let cancelled = false;
    listMappings()
      .then((rows) => {
        if (!cancelled) {
          setMappings(rows);
          setMappingsError(null);
        }
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        // Left as an error rather than an empty table: "the table was unreachable" must
        // never render as "no rules exist", which is the reading that gets a duplicate
        // rule authored over a live one, or a rule "fixed" on the wrong rung.
        setMappings(null);
        setMappingsError(rejectionMessage(err, 'Could not load the routing rules.'));
      });
    return () => {
      cancelled = true;
    };
  }, [reloadToken]);

  useEffect(() => {
    let cancelled = false;
    listDestinations()
      .then((rows) => {
        if (!cancelled) {
          setDestinations(rows);
          setDestinationsError(null);
        }
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setDestinations(null);
        setDestinationsError(rejectionMessage(err, 'Could not load the destinations.'));
      });
    return () => {
      cancelled = true;
    };
  }, [reloadToken]);

  useEffect(() => {
    let cancelled = false;
    getOrganizations({ pageSize: 100 })
      .then((res) => {
        if (cancelled) return;
        setOrgs(res.items.map((o) => ({ id: o.id, name: o.name })));
        // Disclosed rather than silent (#4914): a single page is all this read fetches.
        setOrgsTruncated(res.hasMore);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, []);

  /**
   * The team names behind the team-scoped rules on screen — Issue #4947.
   *
   * One read per org that actually has a team rule, so the cost tracks the table rather
   * than the platform's org count. A read that fails or truncates is recorded as
   * `complete: false`, which suppresses the "team not found" flag: an unresolvable id is
   * only newsworthy when we know the list we compared it against was whole.
   */
  const teamRuleOrgIds = useMemo(
    () => Array.from(new Set((mappings ?? []).filter((m) => m.scope_type === 'team' && m.scope_id_org).map((m) => m.scope_id_org as string))).sort(),
    [mappings]
  );

  useEffect(() => {
    if (!teamRuleOrgIds.length) {
      setTeamIndexes(new Map());
      return;
    }
    let cancelled = false;
    Promise.all(
      teamRuleOrgIds.map((orgId) =>
        getOrgTeams(orgId, { pageSize: 100 })
          .then((res): [string, OrgTeamIndex] => [orgId, { byId: new Map(res.items.map((t) => [t.id, t])), complete: !res.hasMore }])
          .catch((): [string, OrgTeamIndex] => [orgId, { byId: new Map(), complete: false }])
      )
    ).then((entries) => {
      if (!cancelled) setTeamIndexes(new Map(entries));
    });
    return () => {
      cancelled = true;
    };
  }, [teamRuleOrgIds]);

  const handleChanged = useCallback((message: string) => {
    setConfirmation(message);
    setReloadToken((n) => n + 1);
  }, []);

  const handleUnlink = async () => {
    if (!unlinking || unlinkBusy) return;
    setUnlinkBusy(true);
    setUnlinkError(null);
    try {
      await unlinkAwsConnection(unlinking.id);
      setUnlinking(null);
      handleChanged('Bedrock link removed. The original AWS connection is still available to its owner.');
    } catch (err: unknown) {
      setUnlinkError(rejectionMessage(err, 'Could not unlink the connection.'));
    } finally {
      setUnlinkBusy(false);
    }
  };

  const handleVerify = async (destination: DestinationSummary) => {
    setVerifyingId(destination.id);
    try {
      const result = await verifyDestination(destination.id);
      setReloadToken((n) => n + 1);
      if (result.verified) {
        toast.success(`${destination.label} verified.`);
      } else {
        // A verdict, not a transport failure — reported as the reason it gave.
        toast.error(describeRoutingReason(result.reason) ?? `${destination.label} could not be verified.`);
      }
    } catch (err: unknown) {
      toast.error(rejectionMessage(err, 'Could not re-verify that destination.'));
    } finally {
      setVerifyingId(null);
    }
  };

  const visibleMappings = useMemo(
    () => (mappings ?? []).filter((m) => scopeFilter === 'all' || m.scope_type === scopeFilter),
    [mappings, scopeFilter]
  );

  return (
    <section aria-label="Bedrock account routing" data-testid="bedrock-account-routing">
      <Card>
        <div className="space-y-4">
          <div className="flex justify-between items-start gap-4">
            <div>
              <h2 className="text-lg font-semibold text-gray-900 dark:text-white">Bedrock account routing</h2>
              <p className="text-sm text-gray-600 dark:text-gray-400">
                Choose which AWS account serves Bedrock calls, per organization, team, or person. Verified rules apply automatically. The narrowest rule that applies wins: person, then
                team, then organization, then the platform account.
              </p>
            </div>
            <Button onClick={() => setShowAddRule(true)} data-testid="routing-add-rule">
              + Add rule
            </Button>
          </div>

          {/* Ruling 1, unprompted. Surprising if undisclosed, and it is the reason an
              unusable destination deserves attention before somebody is paged. */}
          <Alert variant="info" title="Calls fail closed">
            <p data-testid="routing-fail-closed-banner">
              If a mapped account cannot serve a call — the model is not enabled there, or its role is not assumable — the call fails with a clear
              error. It never silently falls back to the platform account.
            </p>
          </Alert>

          {confirmation && (
            <Alert variant="success" title="Saved" onDismiss={() => setConfirmation(null)}>
              <p data-testid="routing-confirmation">
                {confirmation} Routing picks up the change within about a minute, so a request made immediately afterwards may still use the previous
                account.
              </p>
            </Alert>
          )}

          {/* ------------------------------------------------------------------ */}
          {/* Rules table                                                         */}
          {/* ------------------------------------------------------------------ */}
          <div>
            <div className="flex justify-between items-center border-b border-gray-200 dark:border-gray-700 pb-2 mb-2">
              <h3 className="text-sm font-medium text-gray-900 dark:text-white">Routing rules ({mappings?.length ?? 0})</h3>
              <div className="w-44">
                <Select
                  label=""
                  name="routing-scope-filter"
                  value={scopeFilter}
                  onChange={(e) => setScopeFilter(e.target.value)}
                  options={SCOPE_FILTERS}
                />
              </div>
            </div>

            {mappingsError && (
              <Alert variant="error" title="The routing rules could not be read">
                <p data-testid="routing-mappings-error">{mappingsError} This is not a statement that no rules exist.</p>
              </Alert>
            )}

            {!mappingsError && (
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-left text-gray-500 dark:text-gray-400 border-b border-gray-200 dark:border-gray-700">
                    <th className="py-2 pr-4 font-medium">SCOPE</th>
                    <th className="py-2 pr-4 font-medium">APPLIES TO</th>
                    <th className="py-2 pr-4 font-medium">DESTINATION</th>
                    <th className="py-2 pr-4 font-medium">STATUS</th>
                    <th className="py-2 font-medium" />
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100 dark:divide-gray-800">
                  {visibleMappings.map((mapping) => {
                    const scope = describeMappingScope(mapping, mapping.scope_id_org ? teamIndexes.get(mapping.scope_id_org) : undefined);
                    return (
                    <tr
                      key={mapping.id}
                      // The mockup greys a self-selected row: it is somebody's own choice,
                      // not an admin's rule, and this panel does not author it.
                      className={mapping.source === 'self' ? 'text-gray-400 dark:text-gray-500' : undefined}
                      data-testid={`routing-mapping-${mapping.id}`}
                    >
                      <td className="py-3 pr-4">
                        <span className="bg-gray-100 dark:bg-gray-800 rounded px-2 py-0.5 text-xs font-medium">{mapping.scope_type.toUpperCase()}</span>
                      </td>
                      <td className="py-3 pr-4">
                        {scope.text}
                        {scope.unresolvedTeam && (
                          // Kept visible and flagged, never hidden (#4947): a rule whose
                          // team id names nothing is one the resolver cannot match, so it
                          // governs nobody while reading back as configured routing. An
                          // admin can only re-author it if they can see it.
                          <span
                            className="ml-2 text-xs text-amber-700 dark:text-amber-400"
                            title="This rule's team id does not name a team in this organization, so no request can match it. Re-author the rule against a current team."
                            data-testid={`routing-mapping-team-not-found-${mapping.id}`}
                          >
                            team not found — this rule matches nobody
                          </span>
                        )}
                      </td>
                      <td className="py-3 pr-4 font-mono text-xs">
                        {mapping.destination_label} ({shortAccount(mapping.destination_account_id)})
                        {mapping.source === 'self' && <span className="italic ml-1">(self-selected)</span>}
                      </td>
                      <td className="py-3 pr-4">
                        {mapping.destination_usable ? (
                          <span className="text-green-700 dark:text-green-400">● Verified</span>
                        ) : (
                          // §4.4: the resolver treats this as NO MATCH and walks on, so it
                          // is a warning about where their traffic actually goes — not a
                          // broken row to be deleted.
                          <span className="text-red-700 dark:text-red-400 font-medium" data-testid={`routing-mapping-unusable-${mapping.id}`}>
                            ✗ Destination unusable — these calls fall through to a broader rule
                          </span>
                        )}
                      </td>
                      <td className="py-3 text-right">
                        {mapping.source === 'self' ? (
                          <span
                            className="text-xs italic text-gray-500 dark:text-gray-400"
                            title="Selected by the person on their own credentials page. Add an admin rule for them to override it."
                            data-testid={`routing-managed-by-user-${mapping.id}`}
                          >
                            managed by user
                          </span>
                        ) : (
                          <Button variant="danger" size="sm" onClick={() => setRemoving(mapping)} data-testid={`routing-remove-${mapping.id}`}>
                            Remove
                          </Button>
                        )}
                      </td>
                    </tr>
                    );
                  })}

                  {/* Rung 4 rendered as the fact it is. There is no platform row to
                      author — rung 4 is the ABSENCE of a mapping (§1.2) — so this is a
                      statement about the default, and has no controls. */}
                  <tr className="bg-gray-50 dark:bg-gray-800/50" data-testid="routing-platform-row">
                    <td className="py-3 pr-4">
                      <span className="bg-gray-200 dark:bg-gray-700 rounded px-2 py-0.5 text-xs font-medium">PLATFORM</span>
                    </td>
                    <td className="py-3 pr-4">everyone else</td>
                    <td className="py-3 pr-4 font-mono text-xs">the platform&apos;s own account</td>
                    <td className="py-3 pr-4 text-gray-500 dark:text-gray-400">default</td>
                    <td className="py-3" />
                  </tr>
                </tbody>
              </table>
            )}

            <EffectiveLookup />
          </div>

          {/* ------------------------------------------------------------------ */}
          {/* Destinations table                                                  */}
          {/* ------------------------------------------------------------------ */}
          <div className="pt-2 border-t border-gray-200 dark:border-gray-700">
            <div className="flex justify-between items-center pb-2 mb-2">
              <h3 className="text-sm font-medium text-gray-900 dark:text-white">Connected destinations ({destinations?.length ?? 0})</h3>
              <div className="flex gap-2">
                <Button variant="secondary" size="sm" onClick={() => setShowLink(true)}>Use existing AWS connection</Button>
                <Button variant="secondary" size="sm" onClick={() => setShowRegister(true)} data-testid="routing-register-open">
                  + Register new account
                </Button>
              </div>
            </div>

            {destinationsError && (
              <Alert variant="error" title="The destinations could not be read">
                <p data-testid="routing-destinations-error">{destinationsError}</p>
              </Alert>
            )}

            {!destinationsError && (
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-left text-gray-500 dark:text-gray-400 border-b border-gray-200 dark:border-gray-700">
                    <th className="py-2 pr-4 font-medium">ACCOUNT</th>
                    <th className="py-2 pr-4 font-medium">SOURCE</th>
                    <th className="py-2 pr-4 font-medium">LAST VERIFIED</th>
                    <th className="py-2 pr-4 font-medium">USED BY</th>
                    <th className="py-2 font-medium" />
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100 dark:divide-gray-800">
                  {(destinations ?? []).map((destination) => (
                    <tr
                      key={destination.id}
                      // The mockup's red row. A failed destination is shown, never filtered
                      // away: this table is where a fail-closed outage gets diagnosed.
                      className={!destination.usable_for_routing && destination.reason ? 'bg-red-50 dark:bg-red-900/20' : undefined}
                      data-testid={`routing-destination-${destination.id}`}
                    >
                      <td className="py-3 pr-4 font-mono text-xs">
                        {destination.label} ({shortAccount(destination.account_id)})
                      </td>
                      <td className="py-3 pr-4">
                        {destination.source === 'org-linked'
                          ? `org-linked (${orgs.find((org) => org.id === destination.owner_org_id)?.name || destination.owner_org_id})`
                          : 'admin-registered'}
                        {destination.connection_id && <span className="block text-xs text-gray-500">Existing AWS connection</span>}
                      </td>
                      <td className="py-3 pr-4">
                        <VerificationState destination={destination} />
                      </td>
                      <td className="py-3 pr-4">
                        {destination.used_by} {destination.used_by === 1 ? 'rule' : 'rules'}
                      </td>
                      <td className="py-3 text-right">
                        <Button
                          variant="secondary"
                          size="sm"
                          onClick={() => handleVerify(destination)}
                          disabled={verifyingId === destination.id}
                          data-testid={`routing-verify-${destination.id}`}
                        >
                          {verifyingId === destination.id ? 'Verifying…' : 'Re-verify'}
                        </Button>
                        {destination.connection_id && <Button
                          variant="secondary" size="sm" className="ml-2"
                          disabled={destination.used_by > 0}
                          title={destination.used_by > 0 ? 'Remove routing rules before unlinking' : 'Remove this Bedrock link'}
                          onClick={() => { setUnlinking(destination); setUnlinkError(null); }}
                        >Unlink</Button>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}

            <p className="text-xs text-gray-500 dark:text-gray-400 mt-3" data-testid="routing-destinations-note">
              A destination that fails verification cannot be selected in new rules. Existing rules pointing at it are left in place — the calls they
              cover fall through to a broader rule, and re-verifying is how one comes back into service.
            </p>
          </div>
        </div>
      </Card>

      <AddRuleModal
        isOpen={showAddRule}
        onClose={() => setShowAddRule(false)}
        onSaved={handleChanged}
        orgs={orgs}
        orgsTruncated={orgsTruncated}
        destinations={destinations ?? []}
      />

      <RemoveRuleModal
        mapping={removing}
        teamIndex={removing?.scope_id_org ? teamIndexes.get(removing.scope_id_org) : undefined}
        onClose={() => setRemoving(null)}
        onRemoved={handleChanged}
      />

      {showLink && <LinkAwsConnectionModal onClose={() => setShowLink(false)} onLinked={handleChanged} />}
      <Modal isOpen={!!unlinking} onClose={() => { if (!unlinkBusy) setUnlinking(null); }} title="Unlink AWS connection?">
        <div className="space-y-4">
          <p>Remove the Bedrock link for {unlinking?.label}? The original AWS connection and AWS role will remain unchanged.</p>
          {unlinkError && <Alert variant="error" title="Could not unlink connection">{unlinkError}</Alert>}
          <div className="flex justify-end gap-2">
            <Button variant="secondary" disabled={unlinkBusy} onClick={() => setUnlinking(null)}>Cancel</Button>
            <Button disabled={unlinkBusy} onClick={handleUnlink}>{unlinkBusy ? 'Unlinking…' : 'Unlink connection'}</Button>
          </div>
        </div>
      </Modal>
      <RegisterDestinationModal isOpen={showRegister} onClose={() => setShowRegister(false)} onRegistered={handleChanged} orgs={orgs} />
    </section>
  );
}
