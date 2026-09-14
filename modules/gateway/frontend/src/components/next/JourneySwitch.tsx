/**
 * The Use ADP / Administration switch — Issue #5080 (NUI-02 of EPIC #5078).
 *
 * The control that moves between the two journeys. It is deliberately NOT the
 * "Back to current UI" link: that one leaves the preview entirely and is rendered
 * separately and persistently by `NextLayout`. #5079's layout comment calls this
 * out explicitly — "that switch is a different control and must not replace this
 * one" — so both are present at once and this file adds no return affordance.
 *
 * ## Behaviour
 *
 * - Rendered as links, not buttons, so each journey has a real URL: middle-click,
 *   bookmark and browser back all work, and a deep link into either journey lands
 *   in the right one.
 * - The target is the journey's *remembered* location when the caller supplies one
 *   (a per-journey return location, so leaving Administration returns you where you
 *   were in Use ADP), falling back to the journey home. The caller validates that
 *   remembered path against live permissions before passing it here.
 * - The active journey is marked with `aria-current="page"`, which is how a
 *   screen-reader user knows which journey they are in — colour alone would not say.
 * - When the actor may not enter Administration, the switch renders nothing at all
 *   rather than a disabled tab. A visible-but-dead Administration tab would be a
 *   nonfunctional control presented as a feature, and it would also imply the
 *   person has an administration surface they do not have.
 *
 * A tablist/tab role would be wrong here: these are navigations to separate URLs,
 * not tab panels within one view, so this is a labelled `nav` landmark instead.
 */

import { Link } from 'react-router-dom';
import type { Journey, JourneyId } from './journeys';

interface JourneySwitchProps {
  /** The journeys to offer. Administration is omitted by the caller when the
   *  actor may not enter it. */
  journeys: Journey[];
  /** The journey currently being viewed. */
  active: JourneyId;
  /** Destination per journey — a remembered location or the journey home. */
  destination: (journey: Journey) => string;
  /** Called after a journey link is followed, so the mobile drawer can close. */
  onNavigate?: () => void;
}

export function JourneySwitch({
  journeys,
  active,
  destination,
  onNavigate,
}: JourneySwitchProps) {
  // One journey is not a choice; rendering a lone "Use ADP" tab would suggest a
  // second one exists but is unavailable.
  if (journeys.length < 2) return null;

  return (
    <nav aria-label="Journey" data-testid="next-journey-switch">
      <ul className="flex gap-1 rounded-lg bg-gray-100 p-1 dark:bg-gray-800">
        {journeys.map((journey) => {
          const isActive = journey.id === active;
          return (
            <li key={journey.id} className="flex-1">
              <Link
                to={destination(journey)}
                onClick={onNavigate}
                data-testid={`next-journey-${journey.id}`}
                // Marks the current journey for assistive technology, not just
                // visually.
                aria-current={isActive ? 'page' : undefined}
                title={journey.description}
                className={`block rounded-md px-3 py-1.5 text-center text-sm font-medium transition-colors focus:outline-none focus:ring-2 focus:ring-primary-500 ${
                  isActive
                    ? 'bg-white text-primary-700 shadow-sm dark:bg-gray-900 dark:text-primary-200'
                    : 'text-gray-600 hover:text-gray-900 dark:text-gray-300 dark:hover:text-white'
                }`}
              >
                {journey.label}
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
