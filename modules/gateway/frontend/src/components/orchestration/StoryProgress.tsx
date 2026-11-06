/** Count of accepted stories, not a percentage of effort or deployment readiness. */
export function StoryProgress({ complete, total, scope = 'stories' }: { complete: number; total: number; scope?: string }) {
  if (!total) return null;
  return <div className="space-y-1.5">
    <p className="text-sm font-medium" data-testid="story-completion-count">{complete} of {total} {scope} complete</p>
    <div role="progressbar" aria-label={`${scope} complete`} aria-valuenow={complete} aria-valuemin={0} aria-valuemax={total}
      dir="ltr" className="flex h-2 w-full overflow-hidden rounded-full bg-gray-200 dark:bg-gray-700">
      <div data-testid="story-progress-fill" className="h-full bg-green-600" style={{ width: `${Math.min(100, Math.max(0, complete / total * 100))}%` }} />
    </div>
  </div>;
}
