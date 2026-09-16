/**
 * "Which AWS account serves my Bedrock calls" — Issue #4746 (#4692 · R5), §6.4.
 *
 * Mounted inside the existing **AWS Accounts** section of `/settings/credentials`. Ruling 2
 * is explicit that this is *not* a new screen: the person has just connected an account on
 * this page, and "may I send my model calls through it?" is the next thought they have.
 *
 * §6.4 names three requirements, and each one is a specific misreading it prevents:
 *
 * 1. **Show the EFFECTIVE destination, including when a platform mapping overrides the
 *    person's own selection (§1.4).** *"'Your calls currently go to …1234 (set by a
 *    platform admin)' is honest; showing the user's own stale pick as active is the
 *    inert-config defect."* So this renders what governs, from the server's
 *    `own_selection_active`, and never marks a row active because it is stored.
 *
 * 2. **Only the person's own verified, routing-capable connections may be picked.** The
 *    list is an affordance — the server re-checks every gate on write and answers `422`
 *    — so the point of rendering unselectable rows *with their reason* is that per §5.0b
 *    most existing personal connections are legitimately unselectable (v1-template roles
 *    are pinned to their creator). Hiding them would show an empty list and no
 *    explanation.
 *
 * 3. **Disclose fail-closed (ruling 1, §2.5).** If the chosen account cannot serve a
 *    call, the call *fails* with an explanatory error; there is no quiet fallback to the
 *    platform account. Stated unprompted, before any choice is made, because this is the
 *    one surface where the person making the choice is the person who gets paged by it.
 *
 * Three rendering rules follow the `PersonSpendingLimit` read-only precedent, for the
 * same reasons:
 *
 * - **A load failure is not "your calls go to the platform account".** An outage is the
 *   one moment we cannot know who is billed, so the error state says so rather than
 *   falling back to a specific, plausible, wrong answer.
 * - **The platform rung is an answer, not an absence.** No mapping means today's
 *   ambient-IRSA behaviour, which is complete and correct; rendering it as "unconfigured"
 *   would invite the person to fix something that is not broken.
 * - **When an admin has pinned them, there is no control** — not a disabled one and not
 *   one that 422s. The write is refused server-side (`pinned_by_platform_admin`), and the
 *   honest answer to "how do I change this?" is a person, not a form.
 */

import { useCallback, useEffect, useState } from 'react';
import { AwsConnectionRow } from '@/components/aws/AwsConnectionRow';
import { clearMySelection, getMySelection, setMySelection } from '@/services/bedrockRoutingSelf';
import { describeRoutingReason } from '@/types/bedrockRouting';
import type { MySelectionResponse, SelectableConnection } from '@/types/bedrockRouting';

/** `…1234` — the mockup's account rendering, and §6.4's own copy. */
function shortAccount(accountId: string): string {
  return `…${accountId.slice(-4)}`;
}

/**
 * The human-readable half of a refusal.
 *
 * `apiClient` throws the parsed body, so a `422` from this surface arrives as
 * `{detail: {reason, message}}` — the server's own prose, preferred because it names what
 * was refused and what to do. Falls back through the shared reason vocabulary and then to
 * the transport message; "Could not save" alone tells the person nothing they can act on.
 *
 * A refusal here is a **normal outcome to display**: the save runs a real assume-role
 * probe, and refusing a destination that cannot serve a call — rather than storing a rule
 * that fails every one of them — is the #4511 discipline working.
 */
function refusalMessage(err: unknown, fallback: string): string {
  const detail = (err as { detail?: unknown })?.detail;
  if (typeof detail === 'string' && detail) return detail;
  const structured = detail as { reason?: string; message?: string } | undefined;
  if (structured?.message) return structured.message;
  const described = describeRoutingReason(structured?.reason);
  if (described) return described;
  return (err as { message?: string })?.message || fallback;
}

/**
 * Where the caller's calls actually go, in one sentence.
 *
 * Every branch names a **destination**, because that is the question. The order matters:
 * the admin override is checked first, since a pinned person's own selection is not what
 * is in force and any sentence about their own pick would be the inert-config defect.
 */
function EffectiveDestination({ selection }: { selection: MySelectionResponse }) {
  const { effective } = selection;

  if (selection.pinned_by_platform_admin || effective.overrides_self_selection) {
    return (
      <p className="text-sm text-gray-800" data-testid="bedrock-selection-overridden">
        Your Bedrock calls go to{' '}
        <span className="font-medium">
          account {effective.account_id ? shortAccount(effective.account_id) : 'the platform account'}
        </span>{' '}
        — overridden by a platform mapping. A platform admin chose this, and that choice takes precedence over your own.
      </p>
    );
  }

  if (effective.rung === 'platform') {
    return (
      <p className="text-sm text-gray-800" data-testid="bedrock-selection-platform">
        Your Bedrock calls go to <span className="font-medium">the platform&apos;s AWS account</span>, which is billed for them. That is the default
        and nothing is misconfigured.
      </p>
    );
  }

  if (selection.own_selection_active) {
    return (
      <p className="text-sm text-gray-800" data-testid="bedrock-selection-own-active">
        Your Bedrock calls go to <span className="font-medium">your own account {shortAccount(effective.account_id ?? '')}</span>
        {effective.destination_label ? ` (${effective.destination_label})` : ''}, which is billed for them.
      </p>
    );
  }

  // A rule of somebody else's is serving them: their team's or their org's. Not an
  // absence and not an override of a personal pick — a fact about who pays that the
  // person needs, and the rung they would fall back to if they cleared a selection.
  return (
    <p className="text-sm text-gray-800" data-testid="bedrock-selection-inherited">
      Your Bedrock calls go to{' '}
      <span className="font-medium">account {effective.account_id ? shortAccount(effective.account_id) : 'the platform account'}</span>, set by your{' '}
      {effective.rung === 'team' ? 'team' : 'organization'}&apos;s routing rule.
    </p>
  );
}

/**
 * The §4.4 case, stated separately from everything else.
 *
 * The person's own row still exists and is still theirs — nobody overrode it — but its
 * destination has stopped being usable, so the resolver skips it and walks on. Silence
 * here is precisely the #4511 defect: a stored selection that governs nothing, with a
 * screen that shows it as configured.
 */
function StaleSelectionNotice({ selection }: { selection: MySelectionResponse }) {
  if (selection.pinned_by_platform_admin) return null;
  if (!selection.own_selection_destination_id || selection.own_selection_active) return null;

  return (
    <p className="text-sm text-amber-800" data-testid="bedrock-selection-stale">
      You chose account {selection.own_selection_account_id ? shortAccount(selection.own_selection_account_id) : 'one of your own'}, but it can no
      longer serve Bedrock calls, so it is not in use — the destination above is. Re-run the routing CloudFormation template in that account and
      select it again, or clear the selection.
    </p>
  );
}

/**
 * The fail-closed disclosure (ruling 1, §2.5) — §6.4's third requirement.
 *
 * Rendered **unprompted and before any choice**, not as a warning after a failure. The
 * behaviour is surprising if undisclosed: a person reasonably assumes a broken destination
 * falls back to however things worked before, and it does not. This is the one surface
 * where the person choosing is the person their own agents page at 2am.
 */
function FailClosedNotice() {
  return (
    <p className="text-sm text-blue-800" data-testid="bedrock-selection-fail-closed">
      If the account you choose cannot serve a call, that call fails with an error explaining why — it does not fall back to the platform&apos;s
      account. Your spend moves to your own AWS bill, and so does responsibility for keeping the account able to serve Bedrock.
    </p>
  );
}

/** One pickable (or explained-unpickable) connection. */
function ConnectionChoice({
  connection,
  isActive,
  isBusy,
  onSelect,
}: {
  connection: SelectableConnection;
  isActive: boolean;
  isBusy: boolean;
  onSelect: (credentialId: string) => void;
}) {
  return (
    <AwsConnectionRow
      label={connection.label}
      accountId={connection.account_id}
      status={connection.status}
      dimmed={!connection.selectable}
      testId={`bedrock-selection-connection-${connection.credential_id}`}
      // Why an account cannot be picked, in words that name the remediation. Kept out of
      // the status pill on purpose: `verified` + unroutable is a real and confusing state
      // (§5.0b), and one field cannot say both.
      note={
        !connection.selectable ? (
          <span data-testid={`bedrock-selection-reason-${connection.credential_id}`}>
            Cannot be used for Bedrock routing — {describeRoutingReason(connection.reason) ?? 'this account cannot serve Bedrock calls.'}
          </span>
        ) : null
      }
    >
      {isActive ? (
        <span className="text-sm text-green-700 font-medium" data-testid={`bedrock-selection-active-${connection.credential_id}`}>
          In use
        </span>
      ) : (
        <button
          type="button"
          onClick={() => onSelect(connection.credential_id)}
          // Disabled only for a row the server would refuse anyway. The disable is an
          // affordance; the 422 is the boundary.
          disabled={!connection.selectable || isBusy}
          data-testid={`bedrock-selection-use-${connection.credential_id}`}
          className="text-sm text-blue-600 hover:text-blue-800 disabled:opacity-50 disabled:cursor-not-allowed whitespace-nowrap"
        >
          {isBusy ? 'Saving...' : 'Use this account'}
        </button>
      )}
    </AwsConnectionRow>
  );
}

export function BedrockAccountSelector() {
  const [selection, setSelection] = useState<MySelectionResponse | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  // Distinct from `error`: a refused save is an answer about the destination, while a
  // failed load is an absence of an answer about who is billed. Collapsing them would let
  // a refusal blank out the effective-destination line that is still true.
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const load = useCallback(async () => {
    setIsLoading(true);
    setLoadError(null);
    try {
      setSelection(await getMySelection());
    } catch (err: unknown) {
      // No fallback shape. See the module docstring: an outage must not render as "the
      // platform account serves you".
      setSelection(null);
      setLoadError(refusalMessage(err, 'Could not load which AWS account serves your Bedrock calls.'));
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  /** Re-render from the server's own answer, never from an optimistic local guess. */
  const applyResult = (result: MySelectionResponse) => {
    setSelection(result);
    setSaveError(null);
  };

  const handleSelect = async (credentialId: string) => {
    setBusyId(credentialId);
    setSaveError(null);
    try {
      applyResult(await setMySelection(credentialId));
    } catch (err: unknown) {
      setSaveError(refusalMessage(err, 'That account was not saved as your Bedrock destination.'));
    } finally {
      setBusyId(null);
    }
  };

  const handleClear = async () => {
    setBusyId('clear');
    setSaveError(null);
    try {
      applyResult(await clearMySelection());
    } catch (err: unknown) {
      setSaveError(refusalMessage(err, 'Your selection was not cleared.'));
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div className="mt-4 p-4 bg-gray-50 border border-gray-200 rounded-md" data-testid="bedrock-account-selector">
      <h3 className="text-sm font-semibold mb-2">Bedrock model calls</h3>

      {isLoading && <div className="h-5 bg-gray-200 rounded animate-pulse" data-testid="bedrock-selection-loading" />}

      {/* An outage is not an answer about who is billed. Says so, rather than showing the
          platform-account copy, which would be a specific claim we cannot support. */}
      {!isLoading && loadError && (
        <div>
          <p className="text-sm text-red-700" role="alert" data-testid="bedrock-selection-error">
            {loadError} This is not a statement that your calls go to the platform&apos;s account.
          </p>
          <button
            type="button"
            onClick={() => load()}
            className="mt-2 text-sm text-blue-600 hover:text-blue-800"
            data-testid="bedrock-selection-retry"
          >
            Retry
          </button>
        </div>
      )}

      {!isLoading && !loadError && selection && (
        <div className="space-y-3">
          <EffectiveDestination selection={selection} />
          <StaleSelectionNotice selection={selection} />

          {/* A pinned person gets the disclosure and no control. Not a disabled control:
              the write is refused server-side, so a button here could only 422, and §1.4
              asks the screen to state the override rather than imply it is negotiable. */}
          {selection.pinned_by_platform_admin ? (
            <p className="text-sm text-gray-600" data-testid="bedrock-selection-pinned">
              You cannot change this while a platform admin&apos;s mapping is in place. Ask a platform admin to change or remove it.
            </p>
          ) : (
            <>
              <FailClosedNotice />

              {selection.connections.length === 0 ? (
                <p className="text-sm text-gray-600" data-testid="bedrock-selection-no-connections">
                  Connect an AWS account below to send your Bedrock calls through it instead of the platform&apos;s account.
                </p>
              ) : (
                <div className="space-y-2">
                  {selection.connections.map((connection) => (
                    <ConnectionChoice
                      key={connection.credential_id}
                      connection={connection}
                      // Active means "this is what governs", from the server —
                      // deliberately not "this is the row I stored". Matched on
                      // `own_selection_credential_id`, which the server states: the
                      // mapping's own id is a *destination* id and would never equal a
                      // credential id.
                      isActive={selection.own_selection_active && selection.own_selection_credential_id === connection.credential_id}
                      isBusy={busyId === connection.credential_id}
                      onSelect={handleSelect}
                    />
                  ))}
                </div>
              )}

              {/* Offered whenever a row of theirs exists, including the stale case — a
                  selection that governs nothing is exactly one somebody wants to clear. */}
              {selection.own_selection_destination_id && (
                <div>
                  <button
                    type="button"
                    onClick={handleClear}
                    disabled={busyId === 'clear'}
                    data-testid="bedrock-selection-clear"
                    className="text-sm text-red-600 hover:text-red-800 disabled:opacity-50"
                  >
                    {busyId === 'clear' ? 'Clearing...' : 'Use the default account instead'}
                  </button>
                  {/* The natural reading of "clear" is "my calls will fail". They will
                      not: removing a selection un-shadows the ladder beneath it. */}
                  <p className="text-xs text-gray-500 mt-1">
                    Your calls go back to whichever account your team, organization, or the platform provides. They do not stop working.
                  </p>
                </div>
              )}
            </>
          )}

          {saveError && (
            <p className="text-sm text-red-700" role="alert" data-testid="bedrock-selection-save-error">
              {saveError} Nothing was changed.
            </p>
          )}
        </div>
      )}
    </div>
  );
}
