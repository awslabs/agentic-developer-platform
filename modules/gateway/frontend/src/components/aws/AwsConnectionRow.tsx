/**
 * One connected AWS account, as a row — Issue #4746 (#4692 · R5), §6.6.
 *
 * **This is an extraction, not a new component.** The markup below is the credentials
 * page's own AWS row (`SettingsCredentials.tsx`: link glyph, label, account id, status
 * pill), lifted so the §6.4 Bedrock selector mounted on that same page renders the
 * *identical* row rather than a lookalike. §6.6's rule is "reuse the component, don't
 * copy it", and the two callers here sit inches apart on one screen — which is exactly
 * where a copy is most visible when it drifts: the list of accounts and the list of
 * accounts you may route to would start disagreeing about what an account is called or
 * whether it is verified, on the same page, in the same viewport.
 *
 * **Presentational only, and that is what makes it shareable.** It fetches nothing,
 * mutates nothing, and holds no state. What differs between the two callers is the
 * trailing action (Remove on the credentials list, Use this account on the selector) and
 * whether a reason line is shown, so those are `children` and a prop — not a fork.
 *
 * **No `role_arn` prop, by construction (§2.6).** The role ARN is on no response model
 * this app receives; a prop for one here would invite a future caller to render whatever a
 * server leak put in it. The account id is what identifies an account to a person.
 */

import type { ReactNode } from 'react';

export interface AwsConnectionRowProps {
  /** The person's own nickname for the connection. */
  label: string;
  /** The 12-digit account id, or null when the row has not got one yet. */
  accountId?: string | null;
  /**
   * `verified` | `pending` | `failed`, straight from the server.
   *
   * Rendered as-is rather than mapped to friendlier words: it is the same vocabulary the
   * connect flow and the API use, so a person reading "pending" here and "pending" in a
   * support thread is reading one thing.
   */
  status?: string | null;
  /**
   * An extra line beneath the row — the selector's remediation copy for a connection that
   * cannot be routed to (§5.0b).
   *
   * Separate from `status` because the two answer different questions: `status` is whether
   * the connection works at all, this is why it cannot serve a *routed* call. A verified
   * connection with a v1-template role is `verified` **and** unusable here, and collapsing
   * the two would make that state unreadable.
   */
  note?: ReactNode;
  /** Whether to render the row as unavailable. Visual only — never the control. */
  dimmed?: boolean;
  /** The trailing action: Remove, or Use this account. */
  children?: ReactNode;
  /** Test id for the row itself. */
  testId?: string;
}

export function AwsConnectionRow({ label, accountId, status, note, dimmed = false, children, testId }: AwsConnectionRowProps) {
  const effectiveStatus = status || 'pending';

  return (
    <div
      className={`flex items-center justify-between p-3 bg-white border border-gray-200 rounded-md ${dimmed ? 'opacity-60' : ''}`}
      data-testid={testId}
    >
      <div className="flex items-center gap-3 min-w-0">
        <span className="text-lg">&#x1F517;</span>
        <div className="min-w-0">
          <div>
            <span className="font-medium">{label}</span>
            <span className="text-sm text-gray-500 ml-2">{accountId || ''}</span>
          </div>
          {note && <div className="text-xs text-gray-600 mt-1">{note}</div>}
        </div>
        <span
          className={`text-xs px-2 py-0.5 rounded whitespace-nowrap ${
            effectiveStatus === 'verified' ? 'bg-green-100 text-green-700' : 'bg-yellow-100 text-yellow-700'
          }`}
        >
          {effectiveStatus}
        </span>
      </div>
      {children}
    </div>
  );
}
