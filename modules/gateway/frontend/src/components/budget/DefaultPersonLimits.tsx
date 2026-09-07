/**
 * "Default person limits" — Issue #4691 (#4690 · D2), the admin authoring surface.
 *
 * #4690 gave the platform a rule ladder; this panel is where a platform admin
 * authors it. It answers the question the rest of Budget Management cannot: *how do
 * I bound a whole population's agent spend without waiting for each person to bound
 * themselves?* Every other control on this page caps one entity inside one GitHub
 * org; these rules govern every current AND future member of a scope who has no
 * individual row of their own.
 *
 * The ladder, tightest rung first:
 *
 *     individual row > team default > org default > platform default
 *
 * Four properties of that ladder are load-bearing here, each one a thing this screen
 * would otherwise get wrong:
 *
 * 1. **A rule is read for the scope it is AUTHORED on, never resolved.** `GET
 *    /budget/person-default/{scope}` returns the rule written for that exact scope —
 *    a team with no team-scoped rule reads `uncapped` even while a platform default
 *    governs everybody in it. This panel renders that verbatim and does NOT fall back
 *    to the broader rung, because a screen that showed the platform figure on the
 *    team row would make deleting the team rule look like a no-op.
 *
 * 2. **Removing a rule is not "making the scope unlimited".** Deleting a team rule
 *    leaves that team governed by their org's rule, or the platform's. Only removing
 *    the last applicable rule restores unlimited, and only for people with no
 *    individual row. The remove confirmation says exactly that, because the natural
 *    reading — "I just uncapped these people" — is wrong wherever a broader rule
 *    exists.
 *
 * 3. **Defaults are always enforced.** The route writes `hard` unconditionally and
 *    rejects anything else, so there is no mode to choose and no informational state
 *    to render. (Contrast `PersonSpendingLimit`, whose `soft` rows are a real C3-era
 *    legacy the copy there still has to honour.) Nothing here offers a mode picker:
 *    that would be a control whose only reachable value is the one already in force.
 *
 * 4. **A write takes effect within the enforcement gate's TTL, not instantly** (60s).
 *    On an install whose person-limit tables were both empty, the first rule authored
 *    waits for the process-local existence cache to expire before the person layer
 *    consults these tables at all. The success notice says so — that minute is
 *    precisely when an operator is testing whether the rule works, and silence would
 *    read as "the rule does nothing".
 *
 * **Why there is no table of all authored rules.** The API is per-scope only:
 * `GET/PUT/DELETE /budget/person-default/{scope}`, with no list route. A complete
 * inventory is not derivable client-side — it would mean fanning out over every org ×
 * every team × 3 periods. So this panel shows the platform rung (the one rung
 * enumerable with no ids at all) plus a scope inspector for org and team rungs, and
 * states in the UI that org/team rules are shown as looked up rather than as a
 * complete list. Rendering three fetched rows under a heading that implied
 * completeness would tell an admin "no org defaults exist" when several might — the
 * #4511 inert-cap failure inverted, on the governance surface itself. The list
 * endpoint is filed as follow-up work.
 */

import { useCallback, useEffect, useState } from 'react';
import { Alert, Button, Card, Input, Modal, Select } from '@/components/ui';
import { useToast } from '@/contexts/ToastContext';
import { getOrganizations, getCognitoTeams } from '@/services/admin';
import { getPersonDefault, setPersonDefault, deletePersonDefault } from '@/services/personCap';
import { WORKSPACE_TERM } from '@/utils/budgetVocabulary';
import { formatCurrency } from '@/utils/format';
import type { BudgetPeriodType, PersonDefaultResponse, PersonDefaultScope, PersonDefaultScopeType } from '@/types/budget';

/** The three calendar periods a rule can govern, matching `PersonCapPeriod` server-side. */
const PERIODS: BudgetPeriodType[] = ['daily', 'weekly', 'monthly'];

/** How each period reads in a sentence about a recurring allowance. */
const PERIOD_NOUN: Record<BudgetPeriodType, string> = {
  daily: 'day',
  weekly: 'week',
  monthly: 'month',
};

/**
 * The scope's name in a sentence, using the `WORKSPACE_TERM` vocabulary constant.
 *
 * Built from the RESPONSE's ids, not the form's, so a row always describes the scope
 * the server actually answered for.
 */
function describeScope(scope: PersonDefaultScope): string {
  if (scope.scope_type === 'platform') return 'Everyone on the platform';
  if (scope.scope_type === 'org') return `Everyone in ${WORKSPACE_TERM} ${scope.org}`;
  return `Everyone in team ${scope.team} of ${WORKSPACE_TERM} ${scope.org}`;
}

/** A stable identity for a scope, for React keys and equality checks. */
function scopeKey(scope: PersonDefaultScope): string {
  return [scope.scope_type, scope.org ?? '', scope.team ?? ''].join('|');
}

/**
 * The amount cell for one (scope, period).
 *
 * `cap_status` is the signal, never a truthiness check on `cap_usd`: rule 2 of the
 * cap contract is that "no rule authored" is a distinct state and NOT a `0.00`, and a
 * zero rendered here would read as "nobody in this scope may spend anything" — the
 * most restrictive possible rule where in fact there is none.
 *
 * A load failure is a third state, distinct from both. An outage is the one moment we
 * cannot know whether a rule exists, so it must not render as "none" — that is the
 * reading that gets a duplicate rule authored over a live one.
 */
function DefaultAmount({ row, error }: { row: PersonDefaultResponse | undefined; error: unknown }) {
  if (error) {
    return (
      <span className="text-sm text-red-700 dark:text-red-400" data-testid="person-default-amount-error">
        Could not be read — not a statement that no rule is set
      </span>
    );
  }
  if (!row) {
    return <span className="text-sm text-gray-400 dark:text-gray-500">…</span>;
  }
  if (row.cap_status === 'uncapped' || row.cap_usd == null) {
    return (
      <span className="text-sm text-gray-600 dark:text-gray-400" data-testid="person-default-amount-none">
        No rule set
      </span>
    );
  }
  return (
    <span className="text-sm font-mono text-gray-900 dark:text-white" data-testid="person-default-amount">
      {formatCurrency(Number(row.cap_usd))}
    </span>
  );
}

/**
 * The author/edit modal.
 *
 * No mode picker and no scope picker: the scope is fixed by whoever opened the modal
 * (property 3 above covers the mode). The only decision here is the amount, so the
 * modal contains one field.
 *
 * Validation mirrors the server's constraints (`> 0`, `<= 99999999.99`, 2dp) so a
 * mistake is caught as a form error rather than surfacing as a `422` the operator has
 * to interpret. It is not a substitute for those constraints — the server re-checks.
 * `0` is rejected with the reason spelled out, because "set it to zero to remove it"
 * is the intuition to correct: zero is a real ceiling of zero dollars.
 */
function AuthorDefaultModal({
  isOpen,
  scope,
  period,
  currentAmount,
  onClose,
  onSaved,
}: {
  isOpen: boolean;
  scope: PersonDefaultScope;
  period: BudgetPeriodType;
  currentAmount: string | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const toast = useToast();
  const [amount, setAmount] = useState(currentAmount ?? '');
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  // Re-seed when the modal is reopened for a different row: a stale amount from the
  // previously edited scope would be an authoring mistake pre-filled for the operator.
  useEffect(() => {
    if (isOpen) {
      setAmount(currentAmount ?? '');
      setError(null);
    }
  }, [isOpen, currentAmount, scope, period]);

  const validate = (raw: string): string | null => {
    const trimmed = raw.trim();
    if (!trimmed) return 'Enter an amount.';
    if (!/^\d+(\.\d{1,2})?$/.test(trimmed)) return 'Enter a dollar amount with at most 2 decimal places.';
    const value = Number(trimmed);
    if (value <= 0) {
      return 'A default must be greater than 0. To remove a rule, use Remove — a limit of $0.00 would stop everybody in this scope from spending anything.';
    }
    if (value > 99999999.99) return 'The maximum is $99,999,999.99.';
    return null;
  };

  const handleSave = async () => {
    const problem = validate(amount);
    if (problem) {
      setError(problem);
      return;
    }
    setSaving(true);
    try {
      // 2dp, matching the `NUMERIC(10,2)` column: money crosses the wire at the
      // column's precision rather than as a JS number.
      await setPersonDefault(scope, period, Number(amount.trim()).toFixed(2));
      onSaved();
      onClose();
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Could not save the default limit.';
      // A failed write must not close the modal: closing it would leave the operator
      // believing a population is bounded when nothing was stored.
      setError(message);
      toast.error(message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal isOpen={isOpen} onClose={onClose} title="Default person limit" size="md">
      <div className="space-y-4">
        <p className="text-sm text-gray-600 dark:text-gray-400" data-testid="person-default-modal-scope">
          {describeScope(scope)} — per {PERIOD_NOUN[period]}.
        </p>

        <Input
          label={`Amount (USD) per person, per ${PERIOD_NOUN[period]}`}
          name="person-default-amount"
          type="text"
          inputMode="decimal"
          value={amount}
          onChange={(e) => setAmount(e.target.value)}
          error={error ?? undefined}
          data-testid="person-default-amount-input"
          helperText="Applies to everybody in this scope who has no individual limit and no tighter-scoped default."
        />

        <p className="text-xs text-gray-500 dark:text-gray-400">
          This rule is enforced: once a person's settled spend across every {WORKSPACE_TERM} passes it, their requests and agent runs are stopped
          until the period resets. It governs current and future members of the scope. A person with their own individual limit, or a rule on a
          narrower scope, is unaffected by this one.
        </p>

        <div className="flex justify-end gap-2">
          <Button variant="outline" onClick={onClose} disabled={saving}>
            Cancel
          </Button>
          <Button onClick={handleSave} disabled={saving} data-testid="person-default-save">
            {saving ? 'Saving…' : 'Save default'}
          </Button>
        </div>
      </div>
    </Modal>
  );
}

/**
 * The remove confirmation.
 *
 * Not `DeleteConfirmationModal`: the shared component's copy is "this cannot be
 * undone", and the thing that matters here is different and more surprising —
 * property 2 above. Removing a rule does not uncap anybody who is still covered by a
 * broader rung, and an operator who deletes a team rule expecting a team to go
 * unlimited needs telling that they may have just handed them the org's rule instead.
 */
function RemoveDefaultModal({
  isOpen,
  scope,
  period,
  onClose,
  onRemoved,
}: {
  isOpen: boolean;
  scope: PersonDefaultScope;
  period: BudgetPeriodType;
  onClose: () => void;
  onRemoved: () => void;
}) {
  const toast = useToast();
  const [removing, setRemoving] = useState(false);

  const handleRemove = async () => {
    setRemoving(true);
    try {
      await deletePersonDefault(scope, period);
      onRemoved();
      onClose();
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Could not remove the default limit.';
      toast.error(message);
    } finally {
      setRemoving(false);
    }
  };

  return (
    <Modal isOpen={isOpen} onClose={onClose} title="Remove this default?" size="md">
      <div className="space-y-4">
        <p className="text-sm text-gray-700 dark:text-gray-300" data-testid="person-default-remove-scope">
          {describeScope(scope)} — per {PERIOD_NOUN[period]}.
        </p>

        <Alert variant="warning" title="This may not make anybody unlimited">
          <p data-testid="person-default-remove-ladder">
            Removing this rule does not uncap the people it covered if a broader rule still applies to them — they fall back to the next rule up
            (team, then {WORKSPACE_TERM}, then platform). Only removing the last rule that applies restores unlimited spending, and only for people
            who have no individual limit of their own.
          </p>
        </Alert>

        <div className="flex justify-end gap-2">
          <Button variant="outline" onClick={onClose} disabled={removing}>
            Cancel
          </Button>
          <Button variant="danger" onClick={handleRemove} disabled={removing} data-testid="person-default-remove-confirm">
            {removing ? 'Removing…' : 'Remove default'}
          </Button>
        </div>
      </div>
    </Modal>
  );
}

/**
 * One (scope, period) row: the amount, and the controls to author or remove it.
 *
 * Each row owns its own fetch. That is deliberate rather than a batched load: the
 * scopes on screen are not a fixed set (the inspector adds them), and the API has no
 * multi-scope read, so a shared loader would be a hand-rolled request fan-out with
 * nothing gained. It also means one scope's outage cannot blank the others.
 */
function DefaultRow({
  scope,
  period,
  onChanged,
  reloadToken,
}: {
  scope: PersonDefaultScope;
  period: BudgetPeriodType;
  onChanged: (message: string) => void;
  reloadToken: number;
}) {
  const [row, setRow] = useState<PersonDefaultResponse | undefined>(undefined);
  const [error, setError] = useState<unknown>(null);
  const [showAuthor, setShowAuthor] = useState(false);
  const [showRemove, setShowRemove] = useState(false);

  // Depended on as PRIMITIVES, and the scope object is rebuilt inside the callback
  // from them. Closing over the `scope` prop instead would make this callback's
  // identity depend on an object identity the parent recreates, and the effect below
  // would re-fetch on every parent render — three requests per row, forever.
  const { scope_type: scopeType, org, team } = scope;

  const load = useCallback(async () => {
    setError(null);
    try {
      setRow(await getPersonDefault({ scope_type: scopeType, org, team }, period));
    } catch (err: unknown) {
      // Left as an error state rather than a zeroed row (see `DefaultAmount`).
      setRow(undefined);
      setError(err ?? new Error('unknown'));
    }
  }, [scopeType, org, team, period]);

  useEffect(() => {
    load();
  }, [load, reloadToken]);

  const isSet = row?.cap_status === 'capped' && row.cap_usd != null;

  return (
    <div className="flex items-center justify-between gap-4 py-2 border-b border-gray-100 dark:border-gray-800 last:border-0" data-testid={`person-default-row-${scopeKey(scope)}-${period}`}>
      <div className="min-w-0">
        <span className="text-sm capitalize text-gray-900 dark:text-white">{period}</span>
        <span className="block text-xs text-gray-500 dark:text-gray-400 truncate">per person, per {PERIOD_NOUN[period]}</span>
      </div>

      <div className="flex items-center gap-3">
        <DefaultAmount row={row} error={error} />
        <Button variant="secondary" size="sm" onClick={() => setShowAuthor(true)} data-testid={`person-default-edit-${scopeKey(scope)}-${period}`}>
          {isSet ? 'Edit' : 'Set'}
        </Button>
        {isSet && (
          <Button variant="danger" size="sm" onClick={() => setShowRemove(true)} data-testid={`person-default-remove-${scopeKey(scope)}-${period}`}>
            Remove
          </Button>
        )}
      </div>

      <AuthorDefaultModal
        isOpen={showAuthor}
        scope={scope}
        period={period}
        currentAmount={row?.cap_usd ?? null}
        onClose={() => setShowAuthor(false)}
        onSaved={() => {
          load();
          onChanged(`Default set for ${describeScope(scope).toLowerCase()}, per ${PERIOD_NOUN[period]}.`);
        }}
      />

      <RemoveDefaultModal
        isOpen={showRemove}
        scope={scope}
        period={period}
        onClose={() => setShowRemove(false)}
        onRemoved={() => {
          load();
          onChanged(`Default removed for ${describeScope(scope).toLowerCase()}, per ${PERIOD_NOUN[period]}.`);
        }}
      />
    </div>
  );
}

/**
 * The scope picker: Platform / GitHub org / Team.
 *
 * Orgs come from `getOrganizations` and teams from `getCognitoTeams` — the same
 * server-sourced lists `EntitySelector` uses, never a free-text id. That is the
 * #4511 guard applied to the scope instead of the anchor: a mistyped org id stores a
 * rule that reads back "capped" and governs nobody. The server now rejects a
 * non-existent scope with a `422` (the existence check added in #4696), which is the
 * real guarantee; picking from a list is about not making the mistake in the first
 * place.
 */
function ScopePicker({ onInspect }: { onInspect: (scope: PersonDefaultScope) => void }) {
  const [scopeType, setScopeType] = useState<PersonDefaultScopeType>('org');
  const [orgId, setOrgId] = useState('');
  const [teamId, setTeamId] = useState('');
  const [orgs, setOrgs] = useState<Array<{ id: string; name: string }>>([]);
  const [teams, setTeams] = useState<string[]>([]);

  useEffect(() => {
    let cancelled = false;
    getOrganizations({ pageSize: 100 })
      .then((res) => {
        if (!cancelled) setOrgs(res.items.map((o) => ({ id: o.id, name: o.name })));
      })
      // A picker that cannot load its options is not an error banner's worth of
      // screen: the platform rows above still work, and the note below already says
      // org/team rules must be looked up.
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, []);

  // Teams are per-org, so the list reloads when the org changes and the previously
  // picked team is cleared — a team id from another org is exactly the cross-tenant
  // mistake the two-id scope form exists to prevent.
  useEffect(() => {
    setTeamId('');
    setTeams([]);
    if (scopeType !== 'team' || !orgId) return;
    let cancelled = false;
    getCognitoTeams(orgId, { pageSize: 100 })
      .then((res) => {
        if (!cancelled) setTeams(res.items.map((t) => t.groupName));
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [scopeType, orgId]);

  const ready = scopeType === 'platform' || (scopeType === 'org' && !!orgId) || (scopeType === 'team' && !!orgId && !!teamId);

  return (
    <div className="flex flex-wrap items-end gap-3" data-testid="person-default-scope-picker">
      <div className="w-44">
        <Select
          label="Scope"
          name="person-default-scope-type"
          value={scopeType}
          onChange={(e) => setScopeType(e.target.value as PersonDefaultScopeType)}
          options={[
            { value: 'platform', label: 'Platform' },
            { value: 'org', label: WORKSPACE_TERM },
            { value: 'team', label: 'Team' },
          ]}
        />
      </div>

      {scopeType !== 'platform' && (
        <div className="w-56">
          <Select
            label={WORKSPACE_TERM}
            name="person-default-scope-org"
            value={orgId}
            onChange={(e) => setOrgId(e.target.value)}
            placeholder={`Select a ${WORKSPACE_TERM}`}
            options={orgs.map((o) => ({ value: o.id, label: o.name || o.id }))}
          />
        </div>
      )}

      {scopeType === 'team' && (
        <div className="w-56">
          <Select
            label="Team"
            name="person-default-scope-team"
            value={teamId}
            onChange={(e) => setTeamId(e.target.value)}
            placeholder={orgId ? 'Select a team' : `Select a ${WORKSPACE_TERM} first`}
            disabled={!orgId}
            options={teams.map((t) => ({ value: t, label: t }))}
          />
        </div>
      )}

      <Button
        variant="secondary"
        disabled={!ready}
        data-testid="person-default-inspect"
        onClick={() =>
          onInspect({
            scope_type: scopeType,
            org: scopeType === 'platform' ? undefined : orgId,
            team: scopeType === 'team' ? teamId : undefined,
          })
        }
      >
        Look up rules
      </Button>
    </div>
  );
}

/**
 * The panel. Mounted by Budget Management for platform admins only.
 *
 * The caller does the gating (it knows the caller's role); this component assumes it
 * is only mounted for a platform admin. That gate is an affordance — every route it
 * calls enforces `require_platform_admin` server-side, which is the actual boundary.
 */
export function DefaultPersonLimits() {
  /**
   * Scopes whose rules are on screen.
   *
   * Seeded with the platform rung and only ever grown by the inspector. The platform
   * rung is here because it is the only one addressable with no ids — every other
   * rung needs an org (and a team), which is why they have to be looked up.
   */
  const [scopes, setScopes] = useState<PersonDefaultScope[]>([{ scope_type: 'platform' }]);
  /**
   * Persistent evidence of a write, not a toast.
   *
   * Same reasoning as the #4687 person-limit confirmation on this page: these rules
   * appear in no list a reader can scan for confirmation, so a closing modal plus a
   * vanishing toast reads as a write that did not happen. It also carries the TTL
   * caveat (property 4), which a toast is too short-lived to state.
   */
  const [confirmation, setConfirmation] = useState<string | null>(null);
  /** Bumped to re-read every row after a write — see `DefaultRow`. */
  const [reloadToken, setReloadToken] = useState(0);

  const handleChanged = useCallback((message: string) => {
    setConfirmation(message);
    setReloadToken((n) => n + 1);
  }, []);

  const handleInspect = useCallback((scope: PersonDefaultScope) => {
    setScopes((prev) => (prev.some((s) => scopeKey(s) === scopeKey(scope)) ? prev : [...prev, scope]));
  }, []);

  return (
    <section aria-label="Default person limits" data-testid="default-person-limits">
      <Card>
          <div className="space-y-4">
          <div>
            <h2 className="text-lg font-semibold text-gray-900 dark:text-white">Default person limits</h2>
            <p className="text-sm text-gray-600 dark:text-gray-400">
              Bound what each person may spend across every {WORKSPACE_TERM}, without setting a limit on them one at a time. A default applies to
              everybody in its scope who has no individual limit and no rule on a narrower scope. The narrowest rule that applies wins: an individual
              limit first, then team, then {WORKSPACE_TERM}, then platform.
            </p>
          </div>

          {confirmation && (
            <Alert variant="success" title="Default saved" onDismiss={() => setConfirmation(null)}>
              <p data-testid="person-default-confirmation">
                {confirmation} It governs current and future members of the scope. Enforcement picks up new rules within about a minute, so a request
                made immediately after this change may not be measured against it yet.
              </p>
            </Alert>
          )}

          {scopes.map((scope) => (
            <div key={scopeKey(scope)} data-testid={`person-default-scope-${scopeKey(scope)}`}>
              <h3 className="text-sm font-medium text-gray-900 dark:text-white">{describeScope(scope)}</h3>
              <div className="mt-1">
                {PERIODS.map((period) => (
                  <DefaultRow key={period} scope={scope} period={period} onChanged={handleChanged} reloadToken={reloadToken} />
                ))}
              </div>
            </div>
          ))}

          <div className="pt-2 border-t border-gray-200 dark:border-gray-700 space-y-3">
            <ScopePicker onInspect={handleInspect} />
            {/* The honesty note. Without it, a screen showing only the platform rung
                reads as "no org or team rules exist" — and an admin who concluded that
                would author a platform rule believing nothing narrower could shadow
                it. There is no list endpoint to make this claim properly, so the screen
                states its own limits instead of implying completeness. */}
            <p className="text-xs text-gray-500 dark:text-gray-400" data-testid="person-default-not-a-list">
              Rules for a {WORKSPACE_TERM} or team are shown only once you look them up — this is not a complete list of every rule that exists. A scope
              not shown here has not been checked, which is not the same as having no rule.
            </p>
          </div>
        </div>
      </Card>
    </section>
  );
}
