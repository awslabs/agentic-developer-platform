/** Opens the authenticated explanation feed in Invocation Detail. */
export function LiveStreamLink({ enabled, status, onOpen }: {
  enabled?: boolean; status: string | null; onOpen: () => void;
}) {
  if (!enabled || status !== 'in_progress') return null;
  return <button type="button" className="mr-3 text-sm text-blue-600 dark:text-blue-400 hover:underline"
    onClick={event => { event.stopPropagation(); onOpen(); }}
    onKeyDown={event => event.stopPropagation()}>
    View live stream
  </button>;
}
