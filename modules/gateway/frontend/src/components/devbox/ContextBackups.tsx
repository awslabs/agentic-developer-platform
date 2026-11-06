import { useCrewCheckpoints, type CrewCheckpoint } from '@/hooks/useCrewCheckpoints';

const ARCHIVE_CAP_BYTES = 512 * 1_048_576;

function age(timestamp: string): string {
  const elapsed = Math.max(0, Math.floor((Date.now() - new Date(timestamp).getTime()) / 60_000));
  if (elapsed < 1) return 'just now';
  if (elapsed < 60) return `${elapsed} minute${elapsed === 1 ? '' : 's'} ago`;
  const hours = Math.floor(elapsed / 60);
  if (hours < 24) return `${hours} hour${hours === 1 ? '' : 's'} ago`;
  const days = Math.floor(hours / 24);
  return `${days} day${days === 1 ? '' : 's'} ago`;
}

function captureTime(row: CrewCheckpoint) {
  return row.captured_at && !Number.isNaN(Date.parse(row.captured_at))
    ? <time dateTime={row.captured_at}>{new Date(row.captured_at).toLocaleString()} ({age(row.captured_at)})</time>
    : 'Capture time unavailable';
}

export default function ContextBackups() {
  const { loading, unavailable, checkpoints, failedCapture, selectedId } = useCrewCheckpoints();
  const newestVerifiedSize = checkpoints[0]?.archive_size;
  return <section aria-label="Context backups" className="rounded-xl border border-gray-200 bg-white p-6 shadow-sm dark:border-gray-700 dark:bg-gray-900">
    <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">Context backups</h2>
    <p className="mt-1 text-sm text-gray-600 dark:text-gray-300">
      Captured every 15 minutes while the box runs and before every stop; a Replace restores the marked backup.
      Crew home only (~5 MB), no disk secrets; restores in seconds on any machine.
    </p>
    {loading ? <p role="status" className="mt-3 text-sm">Checking context backups…</p>
      : unavailable ? <p role="alert" className="mt-3 text-sm text-amber-700">Backup status unavailable.</p>
        : <>
          {failedCapture && <p role="alert" className="mt-3 text-sm text-amber-700">
            The newest context backup failed at {captureTime(failedCapture)}. Earlier verified backups may still be available.
          </p>}
          {newestVerifiedSize != null && newestVerifiedSize > ARCHIVE_CAP_BYTES * 0.8 && <p role="alert" className="mt-3 text-sm text-amber-700">
            The newest verified context backup is {(newestVerifiedSize / 1_048_576).toFixed(1)} MB. The gateway cap is 512 MB; captures above the cap will be refused.
          </p>}
          {checkpoints.length === 0 ? <p className="mt-3 text-sm text-amber-700">No backups yet. Your context is not backed up.</p>
            : <ol className="mt-3 space-y-2 text-sm">
              {checkpoints.map(row => <li key={row.id} className="flex flex-wrap items-center gap-x-3">
                <span>{captureTime(row)}</span>
                <span>{row.archive_size == null ? 'Size unavailable' : `${(row.archive_size / 1_048_576).toFixed(1)} MB`}</span>
                {row.id === selectedId && <span className="rounded bg-blue-100 px-2 py-0.5 text-blue-800">Restore point</span>}
              </li>)}
            </ol>}
        </>}
  </section>;
}
