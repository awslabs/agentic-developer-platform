interface PlanSummaryProps {
  stories: number;
  waves: number;
  gates: number;
  evaluations: number;
}

/** Planned implementation work is distinct from its approval and evaluation steps. */
export function PlanSummary({ stories, waves, gates, evaluations }: PlanSummaryProps) {
  return (
    <div className="text-sm text-gray-700 dark:text-gray-300" data-testid="plan-summary">
      <p className="font-medium">
        {stories} {stories === 1 ? 'story' : 'stories'} across {waves} {waves === 1 ? 'wave' : 'waves'}
      </p>
      <p className="text-xs text-gray-500 dark:text-gray-400">
        {gates} approval {gates === 1 ? 'gate' : 'gates'} · {evaluations} {evaluations === 1 ? 'evaluation' : 'evaluations'}
      </p>
    </div>
  );
}
