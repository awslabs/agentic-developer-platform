/**
 * "My spending limit" — Issue #4629 (#4620 · C3).
 *
 * The self-service control on a person's own `/budget` page. It answers one
 * question the rest of this screen cannot: *what is the ceiling on my total agent
 * spend, everywhere?* Every other figure on this page belongs to one organization,
 * so somebody working across two of them can be under a cap in each and under no
 * ceiling on the sum (#4620). This is where they set that ceiling themselves.
 *
 * Three copy rules, each load-bearing rather than stylistic:
 *
 * 1. **It never says spend will be stopped.** The limit is informational in this
 *    release — the figure is reported and nothing is denied (enforcement is
 *    #4630). The same rule the shadow-mode banner follows for the same reason: a
 *    screen that threatens a consequence it cannot deliver trains users to
 *    disbelieve the screen.
 * 2. **No limit is stated as no limit, never as $0.00.** `cap_status` is read
 *    directly; a zero would render a person who may spend nothing, which is the
 *    opposite of the truth.
 * 3. **A load failure is not reported as "no limit".** An outage is the one moment
 *    we cannot know whether a limit exists, so the error state says so explicitly
 *    instead of falling back to the uncapped copy.
 *
 * It also does not show spend, headroom or a progress bar. Those need a
 * *cross-org* denominator, which is #4626; rendering this cap against the
 * single-org spend already on this page would put a number under a bar that does
 * not measure it — the class of wrong figure EPIC #4324 exists to eliminate.
 */

import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Button, Card, Input } from '@/components/ui';
import { deleteMyPersonCap, getMyPersonCap, setMyPersonCap } from '@/services/personCap';
import type { BudgetPeriodType, PersonCapResponse } from '@/types/budget';

const PERIOD_NOUN: Record<BudgetPeriodType, string> = {
  daily: 'day',
  weekly: 'week',
  monthly: 'month',
};

/** `'250.00'` → `'$250.00'`. `null`/empty/unparseable/non-finite → `'—'`, never `'$0.00'`. */
function formatMoney(value: string | null | undefined): string {
  // `Number('')` and `Number('   ')` are 0, not NaN — an empty wire value must
  // render as "unknown", not as a $0.00 ceiling (review fix). Same for
  // 'Infinity'/'1e999', which pass an isNaN check but are not renderable money.
  if (value == null || value.trim() === '') return '—';
  const amount = Number(value);
  if (!Number.isFinite(amount)) return '—';
  // Sign from the ROUNDED value so '-0.004' renders '$0.00', not '-$0.00'.
  const rounded = Number(amount.toFixed(2));
  const sign = rounded < 0 ? '-' : '';
  return `${sign}$${Math.abs(rounded).toFixed(2)}`;
}

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
      This limit is informational for now: your spend is measured and reported against it, but requests are not blocked when it is passed. Use it
      to keep track of your own total.
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

  const {
    data: cap,
    isLoading,
    error,
    refetch,
  } = useQuery<PersonCapResponse>({
    queryKey: ['myPersonCap', period],
    queryFn: () => getMyPersonCap(period),
  });

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
    queryClient.invalidateQueries({ queryKey: ['myPersonCap', period] });
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
      <Card>
        <div className="flex items-start justify-between gap-4 flex-wrap">
          <div>
            <h2 className="text-lg font-semibold text-gray-900 dark:text-white">My spending limit</h2>
            <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
              A ceiling you set on your own agent spend per {PERIOD_NOUN[period]}, counting every organization you work in — not just this one.
            </p>
          </div>
        </div>

        {isLoading && <div className="mt-4 h-10 bg-gray-200 dark:bg-gray-700 rounded animate-pulse" data-testid="person-cap-loading" />}

        {/* A deterministic "no cross-org identity" is NOT an outage (review fix):
            the GET 422s permanently for accounts with no linked GitHub identity,
            and a red alert with a Retry that can never succeed reads as a backend
            failure. Rendered as a calm note, like the page's identity_status
            precedent. */}
        {!isLoading && error && isUnresolvableAnchor(error) && (
          <p className="mt-4 text-sm text-gray-600 dark:text-gray-400" data-testid="person-cap-unlinked">
            A personal limit needs a linked GitHub identity, and this account has none — so there is no cross-organization identity a limit could
            follow. This is a property of the account, not an error.
          </p>
        )}

        {/* An outage is NOT "you have no limit": that is the one moment we cannot
            know, so the uncapped copy would be a claim we cannot support. */}
        {!isLoading && error && !isUnresolvableAnchor(error) && (
          <div className="mt-4">
            <p className="text-sm text-red-700 dark:text-red-400" role="alert" data-testid="person-cap-error">
              Could not load your spending limit. This is not a statement that you have no limit set.
            </p>
            <Button variant="outline" size="sm" className="mt-3" onClick={() => refetch()}>
              Retry
            </Button>
          </div>
        )}

        {!isLoading && !error && cap && (
          <div className="mt-4 space-y-4">
            <div className="flex items-baseline gap-2 flex-wrap" data-testid="person-cap-current">
              {cap.cap_status === 'capped' ? (
                <>
                  <span className="text-2xl font-semibold text-gray-900 dark:text-white font-mono">{formatMoney(cap.cap_usd)}</span>
                  <span className="text-sm text-gray-500 dark:text-gray-400">per {PERIOD_NOUN[period]}, across all organizations</span>
                </>
              ) : (
                <span className="text-sm text-gray-700 dark:text-gray-300" data-testid="person-cap-uncapped">
                  You have not set a personal limit for this period.
                </span>
              )}
            </div>

            {cap.cap_status === 'capped' && cap.enforcement_mode === 'soft' && <InformationalNotice />}

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
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => remove.mutate()}
                    isLoading={remove.isPending}
                    data-testid="person-cap-remove"
                  >
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
      </Card>
    </div>
  );
}
