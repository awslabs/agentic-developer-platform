/**
 * Card-based list view of agent invocations for narrow viewports.
 *
 * Issue #3770: Part of UX EPIC #3753, Wave 3.
 *
 * Renders items as stacked cards instead of a table. Used below the `lg`
 * breakpoint (1024px). The table remains for wider viewports.
 */

import { ActivityCard } from '@/components/activity/ActivityCard';
import type { InvocationItem } from '@/types/activity';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface ActivityCardListProps {
  items: InvocationItem[];
  onDetailClick: (item: InvocationItem) => void;
  onTranscriptClick: (invocationId: string) => void;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function ActivityCardList({ items, onDetailClick, onTranscriptClick }: ActivityCardListProps) {
  return (
    <div
      className="divide-y divide-gray-200 dark:divide-gray-700"
      data-testid="activity-card-list"
      role="list"
      aria-label="Agent activity runs"
    >
      {items.map((item) => (
        <ActivityCard
          key={item.invocation_id}
          item={item}
          onDetailClick={onDetailClick}
          onTranscriptClick={onTranscriptClick}
        />
      ))}
    </div>
  );
}
