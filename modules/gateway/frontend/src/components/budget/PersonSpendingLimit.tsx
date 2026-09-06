/**
 * "My spending limit" — Issue #4629 (#4620 · C3), folded into the Cloud spend tile by #4685.
 *
 * The self-service control on a person's own `/budget` page. It answers one
 * question the rest of this screen cannot: *what is the ceiling on my total agent
 * spend, everywhere?* Every other figure on this page belongs to one workspace,
 * so somebody working across two of them can be under a cap in each and under no
 * ceiling on the sum (#4620). This is where they set that ceiling themselves.
 *
 * **It is a control, not a spend element (#4685).** It shipped as a fifth top-level card
 * that restated the limit figure the Cloud spend tile now carries as its denominator, and
 * the #4669 ruling allows exactly two spend elements on this page. So the figure and the
 * card chrome are gone from here: the tile states the number once, and this renders the
 * mode notices and the editor that authors it. There is exactly one mount point (inside
 * the Cloud tile), which is why there is no variant prop — a second shape would be a
 * second place for the same number to appear.
 *
 * Three copy rules, each load-bearing rather than stylistic:
 *
 * 1. **What it says about stopping matches what the stored row actually does.**
 *    Since #4630 a `hard` row denies, so it says so; a `soft` row (authored under
 *    C3, before enforcement shipped) still does not, so it still says it does not.
 *    Both directions are the same rule: a screen that threatens a consequence it
 *    cannot deliver trains users to disbelieve it, and a screen that silently
 *    acquires a consequence it never mentioned is the same defect mirrored. The
 *    mode is read from the row, never assumed.
 * 2. **No limit is stated as no limit, never as $0.00.** `cap_status` is read
 *    directly; a zero would render a person who may spend nothing, which is the
 *    opposite of the truth.
 * 3. **A load failure is not reported as "no limit".** An outage is the one moment
 *    we cannot know whether a limit exists, so the error state says so explicitly
 *    instead of falling back to the uncapped copy.
 *
 * It shows no spend figure, no headroom and no progress bar. That was #4626's constraint
 * (no cross-workspace denominator existed) and it survives as a *placement* rule: the
 * Cloud spend tile above now has the cross-workspace numerator and draws the bar, and
 * only when the mode is `hard`. Drawing a second one here would either duplicate that bar
 * or — worse — measure this cap against the single-workspace spend beside it, putting a
 * number under a bar that does not measure it.
 */

import { useEffect, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Button, Input } from '@/components/ui';
import { deleteMyPersonCap, setMyPersonCap } from '@/services/personCap';
import { personCapQueryKey, usePersonCap } from '@/hooks/usePersonCap';
import { WORKSPACE_TERM, WORKSPACE_TERM_PLURAL } from '@/utils/budgetVocabulary';
import type { BudgetPeriodType } from '@/types/budget';

const PERIOD_NOUN: Record<BudgetPeriodType, string> = {
  daily: 'day',
  weekly: 'week',
  monthly: 'month',
};

/**
 * Validate the typed amount, mirroring the server's rules.
 *
 * Client-side only as an affordance — the server rejects the same values with a
 * `422` and is the authority. Returns an error string, or `null` when valid.
 *
 * `0` is rejected for the same reason the server rejects it: a zero limit is
 * indistinguishable downstream from no limit, so "I want no limit" is the Remove
 * button, not a `0`.
 */
export function validateCapAmount(raw: string): string | null {
  const trimmed = raw.trim();
  if (!trimmed) return 'Enter an amount.';
  if (!/^\d+(\.\d{1,2})?$/.test(trimmed)) return 'Enter a dollar amount with at most two decimal places, for example 250.00';
  if (Number(trimmed) <= 0) return 'Enter an amount greater than zero. To remove your limit, use Remove limit.';
  // Mirrors the server's `le` bound (NUMERIC(10,2) maximum) so an over-range
  // amount is explained here instead of surfacing as a bare 422.
  if (Number(trimmed) > 99999999.99) return 'Enter an amount of $99,999,999.99 or less.';
  return null;
}

/**
 * The informational-mode notice.
 *
 * Separated out and rendered unconditionally while `enforcement_mode` is `soft`,
 * so the one sentence that must never be dropped is not buried in the middle of a
 * paragraph a later edit might rewrite.
 */
function InformationalNotice() {
  return (
    <p className="text-sm text-blue-800 dark:text-blue-200" data-testid="person-cap-informational">
      This limit is informational: your spend is measured and reported against it, but requests are not blocked when it is passed. Save it again to
      start enforcing it.
    </p>
  );
}

/**
 * The enforcing-mode notice (#4630).
 *
 * Rule 1 above cut both ways and this is the other half of it: C3's rule was
 * "never claim spend will be stopped while nothing stops it", and a cap that now
 * stops spend while the screen stays silent is the same defect mirrored. A person
 * whose agents halt mid-run needs to have been told here that this number does
 * that.
 *
 * It also states the bound rather than promising a hard stop, because the
 * denominator is the settled ledger: spend already incurred but not yet settled is
 * not visible to the check, so a little overshoot is expected and saying otherwise
 * would be the screen over-claiming again.
 */
function EnforcingNotice() {
  return (
    <p className="text-sm text-amber-800 dark:text-amber-200" data-testid="person-cap-enforcing">
      This limit is enforced: once your total agent spend across every {WORKSPACE_TERM} passes it, your agent runs are stopped until the period
      resets or you raise the limit. Spend that has not finished being metered yet is not counted, so the stop can land slightly over the number.
    </p>
  );
}

/** True for the deterministic 422 thrown when the account has no cross-org identity. */
function isUnresolvableAnchor(error: unknown): boolean {
  if (!error || typeof error !== 'object') return false;
  const body = error as { error?: string; detail?: { error?: string } | string };
  const code = body.error ?? (typeof body.detail === 'object' ? body.detail?.error : undefined);
  if (code === 'unresolvable_person_anchor') return true;
  // The API client throws the parsed body; shapes vary by handler, so fall back
  // to the code appearing anywhere in it rather than missing the case.
  try {
    return JSON.stringify(error).includes('unresolvable_person_anchor');
  } catch {
    return false;
  }
}

export function PersonSpendingLimit({ period }: { period: BudgetPeriodType }) {
  const queryClient = useQueryClient();
  const [amount, setAmount] = useState('');
  const [validationError, setValidationError] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);

  // The ONE shared query identity (review fix on #4686): the headline denominator
  // in `MySpend` reads the same hook, so the two cannot drift apart on a save.
  const { data: cap, isLoading, error, refetch } = usePersonCap(period);

  // Seed the field from the stored limit, and re-seed when the period changes —
  // otherwise switching from monthly to daily leaves the monthly figure in the box
  // and a careless Save writes it as the daily limit.
  useEffect(() => {
    // Never while the user is typing (review fix): a background refetch —
    // window-focus refetch, an admin PUT landing, a second tab saving — would
    // otherwise discard the half-typed amount and snap the form shut with no
    // message. The form re-seeds on period change and on edit entry instead.
    if (editing) return;
    setAmount(cap?.cap_usd ?? '');
    setValidationError(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cap?.cap_usd, period]);

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: personCapQueryKey(period) });
  };

  const save = useMutation({
    mutationFn: (value: string) => setMyPersonCap(period, value),
    onSuccess: () => {
      // A fresh success clears the other mutation's stale failure too (review
      // fix): react-query keeps `error` until reset, so a save error surviving a
      // later successful remove would render "not saved" beside the change that
      // just happened.
      remove.reset();
      setEditing(false);
      invalidate();
    },
  });

  const remove = useMutation({
    mutationFn: () => deleteMyPersonCap(period),
    onSuccess: () => {
      save.reset();
      setEditing(false);
      invalidate();
    },
  });

  const onSave = () => {
    const problem = validateCapAmount(amount);
    setValidationError(problem);
    if (problem) return;
    save.mutate(amount.trim());
  };

  const busy = save.isPending || remove.isPending;
  const mutationError = save.error || remove.error;

  return (
    <div data-testid="person-spending-limit">
      {/* No `Card` and no figure of its own (#4685): the Cloud spend tile this is mounted
          inside states the limit once, as its denominator. A second rendering of the same
          number is the ambiguity the #4669 ruling removed. */}
      {isLoading && <div className="h-10 bg-gray-200 dark:bg-gray-700 rounded animate-pulse" data-testid="person-cap-loading" />}

      {/* A deterministic "no cross-workspace identity" is NOT an outage (review fix):
          the GET 422s permanently for accounts with no linked GitHub identity,
          and a red alert with a Retry that can never succeed reads as a backend
          failure. Rendered as a calm note, like the page's identity_status
          precedent. */}
      {!isLoading && error && isUnresolvableAnchor(error) && (
        <p className="text-sm text-gray-600 dark:text-gray-400" data-testid="person-cap-unlinked">
          A personal limit needs a linked GitHub identity, and this account has none — so there is no cross-{WORKSPACE_TERM} identity a limit could
          follow. This is a property of the account, not an error.
        </p>
      )}

      {/* An outage is NOT "you have no limit": that is the one moment we cannot
          know, so the uncapped copy would be a claim we cannot support. */}
      {!isLoading && error && !isUnresolvableAnchor(error) && (
        <div>
          <p className="text-sm text-red-700 dark:text-red-400" role="alert" data-testid="person-cap-error">
            Could not load your spending limit. This is not a statement that you have no limit set.
          </p>
          <Button variant="outline" size="sm" className="mt-3" onClick={() => refetch()}>
            Retry
          </Button>
        </div>
      )}

      {!isLoading && !error && cap && (
        <div className="space-y-3" data-testid="person-cap-current">
          {/* "No limit" still needs saying in words — the tile renders the figure with no
              denominator beside it, and silence there is indistinguishable from a
              denominator that failed to load. Never `$0.00`: `cap_status` is the signal. */}
          {cap.cap_status === 'uncapped' && (
            <p className="text-sm text-gray-700 dark:text-gray-300" data-testid="person-cap-uncapped">
              You have not set a personal limit for this period, so nothing caps your agent spend across all {WORKSPACE_TERM_PLURAL}.
            </p>
          )}

          {cap.cap_status === 'capped' && cap.enforcement_mode === 'soft' && <InformationalNotice />}
          {cap.cap_status === 'capped' && cap.enforcement_mode === 'hard' && <EnforcingNotice />}

          {editing ? (
            <div className="space-y-3">
              <Input
                label={`Limit per ${PERIOD_NOUN[period]} (USD)`}
                name="person-cap-amount"
                data-testid="person-cap-input"
                inputMode="decimal"
                value={amount}
                onChange={(event) => setAmount(event.target.value)}
                error={validationError ?? undefined}
                helperText="For example 250.00. To remove your limit entirely, use Remove limit."
              />
              {/* The mutation's own failure, kept distinct from a validation
                  problem: a failed save must not look like a saved limit. */}
              {mutationError && (
                <p className="text-sm text-red-700 dark:text-red-400" role="alert" data-testid="person-cap-save-error">
                  Your limit was not saved. Nothing has changed — try again.
                </p>
              )}
              <div className="flex gap-2">
                <Button size="sm" onClick={onSave} isLoading={save.isPending} data-testid="person-cap-save">
                  Save limit
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => {
                    save.reset();
                    remove.reset();
                    setEditing(false);
                    setAmount(cap.cap_usd ?? '');
                    setValidationError(null);
                  }}
                  disabled={busy}
                  data-testid="person-cap-cancel"
                >
                  Cancel
                </Button>
              </div>
            </div>
          ) : (
            <div className="flex gap-2 flex-wrap">
              <Button
                variant="outline"
                size="sm"
                onClick={() => {
                  save.reset();
                  remove.reset();
                  setAmount(cap.cap_usd ?? '');
                  setValidationError(null);
                  setEditing(true);
                }}
                data-testid="person-cap-edit"
              >
                {cap.cap_status === 'capped' ? 'Change limit' : 'Set a limit'}
              </Button>
              {cap.cap_status === 'capped' && (
                <Button variant="ghost" size="sm" onClick={() => remove.mutate()} isLoading={remove.isPending} data-testid="person-cap-remove">
                  Remove limit
                </Button>
              )}
            </div>
          )}

          {!editing && mutationError && (
            <p className="text-sm text-red-700 dark:text-red-400" role="alert" data-testid="person-cap-mutation-error">
              That change was not saved. Your limit is unchanged.
            </p>
          )}
        </div>
      )}
    </div>
  );
}
