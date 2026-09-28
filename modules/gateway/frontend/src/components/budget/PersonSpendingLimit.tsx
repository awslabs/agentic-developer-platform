/**
 * "My spending limit" — Issue #4629 (#4620 · C3), folded into the Cloud spend tile
 * by #4685, made READ-ONLY by the #4690 ruling.
 *
 * The person-facing view on their own `/budget` page. It answers one question the
 * rest of this screen cannot: *what is the ceiling on my total agent spend,
 * everywhere?* Every other figure on this page belongs to one workspace, so
 * somebody working across two of them can be under a cap in each and under no
 * ceiling on the sum (#4620).
 *
 * **It no longer authors anything.** Person limits are admin-governed only (the
 * 2026-09-07 operator ruling on #4690): the self-service `PUT`/`DELETE` routes were
 * deleted server-side, so an editor here would be a form whose Save can only 405.
 * This renders the limit that GOVERNS the caller — their individual row if a
 * platform admin (or their own pre-ruling save) authored one, otherwise the most
 * specific team/org/platform default — plus where it came from, and who to ask to
 * change it. Authoring lives on the Budget Management admin screen (#4687, #4691).
 *
 * **It is a status line, not a spend element (#4685).** The #4669 ruling allows
 * exactly two spend elements on this page, and the Cloud spend tile this mounts
 * inside states the limit figure once, as its denominator. So no figure, no
 * headroom, no bar here — only the mode notice, the provenance, and the
 * "managed by admins" line.
 *
 * Three copy rules, each load-bearing rather than stylistic:
 *
 * 1. **What it says about stopping matches what the stored row actually does.**
 *    Since #4630 a `hard` row denies, so it says so; a `soft` row (authored under
 *    C3, before enforcement shipped) still does not, so it still says it does not.
 *    The mode is read from the response, never assumed.
 * 2. **No limit is stated as no limit, never as $0.00.** `cap_status` is read
 *    directly; a zero would render a person who may spend nothing, which is the
 *    opposite of the truth.
 * 3. **A load failure is not reported as "no limit".** An outage is the one moment
 *    we cannot know whether a limit exists, so the error state says so explicitly
 *    instead of falling back to the uncapped copy.
 */

import { Button } from '@/components/ui';
import { usePersonCap } from '@/hooks/usePersonCap';
import { WORKSPACE_TERM, WORKSPACE_TERM_PLURAL } from '@/utils/budgetVocabulary';
import type { BudgetPeriodType } from '@/types/budget';

/**
 * The informational-mode notice.
 *
 * Separated out and rendered unconditionally while `enforcement_mode` is `soft`,
 * so the one sentence that must never be dropped is not buried in the middle of a
 * paragraph a later edit might rewrite. Soft rows are the C3-era legacy: only
 * pre-ruling self-authored rows can carry the mode (admin writes and defaults are
 * hard), so this copy survives solely for them.
 */
function InformationalNotice() {
  return (
    <p className="text-sm text-blue-800 dark:text-blue-200" data-testid="person-cap-informational">
      This limit is informational: your spend is measured and reported against it, but requests are not blocked when it is passed.
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
      This limit is enforced: once everything you spend — your own requests plus your agents' — across every {WORKSPACE_TERM} passes it, further
      requests and agent runs are stopped until the period resets or a platform admin raises the limit. Spend that has not finished being metered yet
      is not counted, so the stop can land slightly over the number.
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
  // The ONE shared query identity (review fix on #4686): the headline denominator
  // in `MySpend` reads the same hook, so the two cannot drift apart.
  const { data: cap, isLoading, error, refetch } = usePersonCap(period);

  return (
    <div data-testid="person-spending-limit">
      {/* No `Card` and no figure of its own (#4685): the Cloud spend tile this is mounted
          inside states the limit once, as its denominator. A second rendering of the same
          number is the ambiguity the #4669 ruling removed. */}
      {isLoading && <div className="h-10 bg-gray-200 dark:bg-gray-700 rounded animate-pulse" data-testid="person-cap-loading" />}

      {/* A deterministic "no cross-workspace identity" is NOT an outage: since #4690
          the GET resolves linked-identity-less accounts through their canonical user
          id, so this 422 survives only for accounts with no user record at all — for
          which a red alert with a Retry that can never succeed would read as a
          backend failure. Rendered as a calm note, like the page's identity_status
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
              No limit applies to you for this period — nothing caps your total spend, direct use or agent runs, across all {WORKSPACE_TERM_PLURAL}.
            </p>
          )}

          {cap.cap_status === 'capped' && cap.enforcement_mode === 'soft' && <InformationalNotice />}
          {cap.cap_status === 'capped' && cap.enforcement_mode === 'hard' && <EnforcingNotice />}

          {/* Provenance, verbatim from the server (`source_label`): "a limit set for
              you by a platform administrator", "platform default", "org default for
              <org>", … The server composes it because only the ladder resolver knows
              which rung won — recomputing it here is how the label and the enforced
              rung drift apart (#4511). */}
          {cap.cap_status === 'capped' && cap.source_label && (
            <p className="text-sm text-gray-600 dark:text-gray-400" data-testid="person-cap-source">
              Where this number comes from: {cap.source_label}.
            </p>
          )}

          {/* The one affordance-shaped sentence left, and it points away from this
              screen on purpose: the ruling removed self-service, so the honest
              answer to "how do I change this?" is a person, not a form. */}
          <p className="text-sm text-gray-600 dark:text-gray-400" data-testid="person-cap-managed">
            Spending limits are managed by platform admins — contact one to request a change.
          </p>
        </div>
      )}
    </div>
  );
}
