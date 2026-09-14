/**
 * The new UI's navigation list — Issue #5080 (NUI-02 of EPIC #5078).
 *
 * Renders the active journey's sections. Every entry comes from the gated model in
 * `journeys.ts`; this component adds no predicates of its own, so there is no
 * second place where "who sees Budgets" could drift from the current sidebar.
 *
 * ## Two labels that are acceptance criteria, not decoration
 *
 * 1. **"Opens in the current UI"** on every entry that leaves the preview. This
 *    story migrates no capability, so every capability link goes to the working
 *    current-UI page. Without the label those links read as preview pages that
 *    happen to look different, which is precisely the "misleading live controls"
 *    the criteria forbid.
 * 2. **"Platform-wide"** on entries whose data is not filtered by the selected
 *    organization. The design note requires system-wide screens to identify their
 *    scope; an unlabelled System health sitting under an organization selector
 *    implies it shows that organization's health.
 *
 * Both are rendered as text, not title attributes or colour, so they survive on
 * mobile and in a screen reader.
 *
 * ## Accessibility
 *
 * The list is a `nav` landmark labelled with the journey name, so a screen-reader
 * user moving by landmark hears "Use ADP navigation" rather than a second
 * unlabelled "navigation" competing with the journey switch. Sections are real
 * headings associated with their list via `aria-labelledby`. Entries are ordinary
 * links in DOM order, which is what makes Tab traversal work without any key
 * handling of our own — a roving-tabindex widget here would remove the browser
 * behaviour it imitates.
 *
 * Active state uses `aria-current="page"`, matched on the *entry id* rather than by
 * comparing hrefs, because two entries legitimately share a destination while a
 * capability still lives inside another page (personal Model access is on the
 * Credentials page today). Matching on href would light up both.
 */

import { Link } from 'react-router-dom';
import { handleGitlabSsoClick } from '@/services/gitlabSso';
import type { Journey, JourneyEntry } from './journeys';

interface NextNavProps {
  journey: Journey;
  /** Id of the entry representing the page being viewed, when any. */
  activeEntryId?: string;
  /** Called after an entry is followed, so the mobile drawer can close. */
  onNavigate?: () => void;
}

function EntryLink({
  entry,
  isActive,
  onNavigate,
}: {
  entry: JourneyEntry;
  isActive: boolean;
  onNavigate?: () => void;
}) {
  const className = `block rounded-md px-3 py-2 focus:outline-none focus:ring-2 focus:ring-primary-500 ${
    isActive
      ? 'bg-primary-50 text-primary-800 dark:bg-primary-900 dark:text-primary-100'
      : 'text-gray-700 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-800'
  }`;

  const body = (
    <>
      <span className="flex flex-wrap items-center gap-2">
        <span className="text-sm font-medium">{entry.label}</span>
        {entry.scope === 'platform' && (
          // The scope of the data behind this link, not a permission claim.
          <span className="rounded bg-gray-200 px-1.5 py-0.5 text-xs font-normal text-gray-700 dark:bg-gray-700 dark:text-gray-200">
            Platform-wide
          </span>
        )}
      </span>
      {entry.currentUi && (
        <span className="mt-0.5 block text-xs text-gray-500 dark:text-gray-400">
          Opens in the current UI
        </span>
      )}
    </>
  );

  // A server-owned path must leave the SPA. A router `Link` would push client-side,
  // match nothing and land on the /next catch-all 404 instead of the real page
  // (#5123). GitLab additionally needs its authenticated SSO handoff, which is the
  // same handler the current sidebar uses.
  if (entry.external) {
    return (
      <a
        href={entry.to}
        onClick={(event) => {
          if (entry.id === 'gitlab') handleGitlabSsoClick(event);
          onNavigate?.();
        }}
        data-testid={`next-nav-entry-${entry.id}`}
        aria-current={isActive ? 'page' : undefined}
        className={className}
      >
        {body}
      </a>
    );
  }

  return (
    <Link
      to={entry.to}
      onClick={onNavigate}
      data-testid={`next-nav-entry-${entry.id}`}
      aria-current={isActive ? 'page' : undefined}
      className={className}
    >
      {body}
    </Link>
  );
}

export function NextNav({ journey, activeEntryId, onNavigate }: NextNavProps) {
  return (
    <nav aria-label={`${journey.label} navigation`} data-testid="next-nav">
      <ul className="space-y-4">
        {journey.sections.map((section) => {
          const headingId = `next-nav-${journey.id}-${section.title
            .toLowerCase()
            .replace(/[^a-z0-9]+/g, '-')}`;
          return (
            <li key={section.title}>
              <h3
                id={headingId}
                className="px-3 pb-1 text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400"
              >
                {section.title}
              </h3>
              <ul aria-labelledby={headingId} className="space-y-0.5">
                {section.entries.map((entry) => (
                  <li key={entry.id}>
                    <EntryLink
                      entry={entry}
                      isActive={entry.id === activeEntryId}
                      onNavigate={onNavigate}
                    />
                  </li>
                ))}
              </ul>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
