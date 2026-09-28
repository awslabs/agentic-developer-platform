/**
 * DraftPanel — live intent draft beside the intake conversation (#4208).
 *
 * The intent-refinement agent calls `update_draft` as facts firm up; the worker
 * emits the whole draft as a STATE_DELTA patch at `/draft`, which `useAgUiEvents`
 * folds into `sessionMeta`. This renders whatever is there right now.
 *
 * Deliberately dumb: no local state, no fetching, no editing. The draft's single
 * source of truth is the agent, so the panel is a pure function of `draft`.
 */

import type { IntentDraft } from '@/types/ag-ui-events';

interface DraftPanelProps {
  draft?: IntentDraft;
}

/** True when the draft has no content worth showing yet. */
function isEmpty(draft: IntentDraft): boolean {
  return (
    !draft.epicDisplay &&
    !draft.waveDisplay &&
    !draft.intent &&
    !draft.motivation &&
    !draft.outcomes?.length &&
    !draft.constraints?.length &&
    !draft.openQuestions?.length
  );
}

export function DraftPanel({ draft }: DraftPanelProps) {
  // Nothing pinned yet, or the agent cleared it — render nothing rather than an
  // empty box, so non-intake conversations are visually unchanged.
  if (!draft || isEmpty(draft)) return null;

  return (
    <aside
      className="px-4 py-3 text-xs border-t border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800/30"
      aria-label="Intent draft"
      data-testid="draft-panel"
    >
      <h2 className="text-[11px] font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400 mb-2">
        Draft intent
      </h2>

      {draft.epicDisplay && <Field label={draft.epicDisplay.title} value={draft.epicDisplay.description} />}
      {draft.waveDisplay && <Field label={draft.waveDisplay.title} value={draft.waveDisplay.description} />}
      {draft.intent && <Field label="Intent" value={draft.intent} />}
      {draft.motivation && <Field label="Motivation" value={draft.motivation} />}
      <ListField label="Outcomes" items={draft.outcomes} />
      <ListField label="Constraints" items={draft.constraints} />
      <ListField label="Open questions" items={draft.openQuestions} />

      {draft.updatedAt && (
        <p className="mt-2 text-[11px] text-gray-400 dark:text-gray-500">
          Updated {draft.updatedAt}
        </p>
      )}
    </aside>
  );
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div className="mb-2">
      <div className="text-gray-500 dark:text-gray-400">{label}</div>
      <p className="text-gray-800 dark:text-gray-200 whitespace-pre-wrap">{value}</p>
    </div>
  );
}

function ListField({ label, items }: { label: string; items?: string[] }) {
  if (!items || items.length === 0) return null;
  return (
    <div className="mb-2">
      <div className="text-gray-500 dark:text-gray-400">{label}</div>
      <ul className="list-disc list-inside text-gray-800 dark:text-gray-200">
        {items.map((item, i) => (
          <li key={`${i}-${item}`} className="whitespace-pre-wrap">
            {item}
          </li>
        ))}
      </ul>
    </div>
  );
}
