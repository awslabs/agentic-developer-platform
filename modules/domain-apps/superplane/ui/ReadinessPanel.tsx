/**
 * The three readiness readings, rendered as three separate statements — #5730.
 *
 * WHY THIS IS NOT A SINGLE BADGE
 * ------------------------------
 * A single green dot beside a workspace is the most natural thing to build here
 * and it is the defect AC-04 forbids: "Control-plane health alone never marks a
 * workspace execution-ready." The control plane answering `GET /health` says the
 * API process is up. It says nothing about whether this workspace has a
 * reconciling controller, a valid provider credential, or any capacity. A user who
 * reads one green badge launches work, it fails, and the platform has spent their
 * trust on a claim it never checked.
 *
 * So each reading gets its own row, its own verdict word, and its own reason.
 *
 * WHY "UNKNOWN" LOOKS DIFFERENT FROM "NOT READY"
 * ----------------------------------------------
 * `ready: null` and `ready: false` are rendered distinctly, in different colours,
 * with different words. "Not ready" invites the user to fix something. "Unknown"
 * invites them to check something — and sometimes there is nothing to fix at all,
 * because the reading was never taken or the route is not deployed in this
 * environment. Collapsing them into one amber blob would make a correct,
 * freshly-installed control plane look broken.
 */

import type { Reading, ReadinessReport } from './readiness';

/** How one reading is described. Verdict and tone are derived together. */
function describe(reading: Reading): {
  verdict: string;
  tone: 'ready' | 'blocked' | 'unknown';
} {
  if (reading.ready === true) return { verdict: 'Ready', tone: 'ready' };
  if (reading.ready === false) return { verdict: 'Not ready', tone: 'blocked' };
  return { verdict: 'Unknown', tone: 'unknown' };
}

const TONE_CLASSES: Record<'ready' | 'blocked' | 'unknown', string> = {
  ready: 'bg-green-100 text-green-800 dark:bg-green-900/40 dark:text-green-200',
  blocked: 'bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-200',
  unknown: 'bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-200',
};

/**
 * A shape as well as a colour for each verdict.
 *
 * Colour alone excludes anyone who cannot distinguish these hues, and readiness
 * is exactly the kind of information that must not depend on that. The glyph is
 * `aria-hidden` because the verdict word beside it already carries the meaning to
 * a screen reader.
 */
const TONE_GLYPH: Record<'ready' | 'blocked' | 'unknown', string> = {
  ready: '✓',
  blocked: '✕',
  unknown: '?',
};

function FreshnessNote({ reading }: { reading: Reading }) {
  if (reading.freshness === 'fresh') return null;
  // A stale reading is reported as stale rather than presented as current. "This
  // was true twenty minutes ago" and "this is true" are different facts and only
  // one of them justifies starting a job.
  const text =
    reading.freshness === 'stale'
      ? `Last observed ${reading.observedAt ?? 'at an unknown time'} — this reading may be out of date.`
      : 'No observation has been recorded.';
  return <p className="mt-1 text-xs text-amber-700 dark:text-amber-300">{text}</p>;
}

function ReadingRow({
  label,
  scope,
  reading,
}: {
  label: string;
  scope: string;
  reading: Reading;
}) {
  const { verdict, tone } = describe(reading);
  return (
    <li className="border-t border-gray-200 py-3 first:border-t-0 dark:border-gray-700">
      {/* Stacks on narrow screens, aligns on wide ones (AC-05 narrow layout). */}
      <div className="flex flex-col gap-1 sm:flex-row sm:items-start sm:justify-between sm:gap-4">
        <div className="min-w-0">
          <p className="font-medium text-gray-900 dark:text-white">{label}</p>
          <p className="text-xs text-gray-500 dark:text-gray-400">{scope}</p>
        </div>
        <span
          className={`inline-flex shrink-0 items-center gap-1 self-start rounded-full px-2.5 py-0.5 text-xs font-medium ${TONE_CLASSES[tone]}`}
        >
          <span aria-hidden="true">{TONE_GLYPH[tone]}</span>
          {verdict}
        </span>
      </div>
      <p className="mt-1 text-sm break-words text-gray-600 dark:text-gray-300">{reading.reason}</p>
      <FreshnessNote reading={reading} />
    </li>
  );
}

/**
 * What each reading actually covers.
 *
 * Spelled out beside every row because the distinction is the entire point and a
 * label like "Workspace" alone does not convey it. These sentences are what stop
 * a reader from assuming the first green row means they can run something.
 */
const SCOPES = {
  controlPlane: 'The Superplane API process. Does not cover running work.',
  workspace: 'This workspace’s ability to accept work.',
  provider: 'The provider credential this workspace would use.',
} as const;

export function ReadinessPanel({
  report,
  workspaceName,
}: {
  report: ReadinessReport;
  workspaceName?: string;
}) {
  return (
    <section
      aria-labelledby="superplane-readiness-heading"
      className="rounded-lg border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-800"
    >
      <h2
        id="superplane-readiness-heading"
        className="text-base font-semibold text-gray-900 dark:text-white"
      >
        Readiness
      </h2>
      <p className="mt-1 text-sm text-gray-600 dark:text-gray-400">
        Reported separately, because a healthy control plane does not mean a workspace can run
        work.
      </p>
      <ul className="mt-3">
        <ReadingRow
          label="Control plane"
          scope={SCOPES.controlPlane}
          reading={report.controlPlane}
        />
        <ReadingRow
          label={workspaceName ? `Workspace “${workspaceName}”` : 'Workspace'}
          scope={SCOPES.workspace}
          reading={report.workspace}
        />
        <ReadingRow
          label="Provider connection"
          scope={SCOPES.provider}
          reading={report.provider}
        />
      </ul>
    </section>
  );
}

export default ReadinessPanel;
