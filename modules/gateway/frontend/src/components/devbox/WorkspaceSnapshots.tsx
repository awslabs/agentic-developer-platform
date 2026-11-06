import { useEffect, useState } from 'react';
import { apiClient } from '@/services/api';

export type SnapshotSummary = {
  snapshot_id: string;
  size_gb: number | null;
  state: string;
  created_at: string | null;
  observed_at: string | null;
};

type SnapshotState = { loading: boolean; unavailable: boolean; items: SnapshotSummary[] };

export default function WorkspaceSnapshots({ desktopId, onSnapshotsChange }: { desktopId: string | null; onSnapshotsChange?: (items: SnapshotSummary[]) => void }) {
  const [state, setState] = useState<SnapshotState>({ loading: true, unavailable: false, items: [] });

  useEffect(() => {
    let active = true;
    let requestId = 0;
    if (!desktopId) {
      setState({ loading: false, unavailable: false, items: [] });
      onSnapshotsChange?.([]);
      return;
    }
    const refresh = async () => {
      const current = ++requestId;
      setState({ loading: true, unavailable: false, items: [] });
      try {
        const listing = await apiClient.get<{ items: SnapshotSummary[] }>(`/cloud-desktops/${encodeURIComponent(desktopId)}/snapshots`);
        if (active && current === requestId) {
          setState({ loading: false, unavailable: false, items: listing.items });
          onSnapshotsChange?.(listing.items);
        }
      } catch {
        if (active && current === requestId) {
          setState({ loading: false, unavailable: true, items: [] });
          onSnapshotsChange?.([]);
        }
      }
    };
    void refresh();
    window.addEventListener('focus', refresh);
    return () => { active = false; window.removeEventListener('focus', refresh); };
  }, [desktopId, onSnapshotsChange]);

  return <section aria-label="Workspace snapshots" className="rounded-xl border border-gray-200 bg-white p-6 shadow-sm dark:border-gray-700 dark:bg-gray-900">
    <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">Workspace snapshots</h2>
    <p className="mt-1 text-sm text-gray-600 dark:text-gray-300">
      Daily encrypted whole-disk snapshots can contain anything on disk, including secrets. Restore a chosen snapshot during Replace
      into the new machine&apos;s zone; recovery takes minutes. Unlike Crew context backups, these are not portable instant restores.
    </p>
    {state.loading ? <p role="status" className="mt-3 text-sm">Checking workspace snapshots…</p>
      : state.unavailable ? <p role="alert" className="mt-3 text-sm text-amber-700">Snapshot status unavailable.</p>
        : state.items.length === 0 ? <p className="mt-3 text-sm text-amber-700">No workspace snapshots yet.</p>
          : <ol className="mt-3 space-y-2 text-sm">
            {state.items.map(snapshot => <li key={snapshot.snapshot_id} className="flex flex-wrap items-center gap-x-3">
              <span>{snapshot.created_at && !Number.isNaN(Date.parse(snapshot.created_at))
                ? <>Created <time dateTime={snapshot.created_at}>{new Date(snapshot.created_at).toLocaleString()}</time></>
                : 'Creation time unavailable'}</span>
              <span>{snapshot.observed_at && !Number.isNaN(Date.parse(snapshot.observed_at))
                ? <>Last checked <time dateTime={snapshot.observed_at}>{new Date(snapshot.observed_at).toLocaleString()}</time></>
                : 'Last checked: unavailable'}</span>
              <span>{snapshot.size_gb == null ? 'Size unavailable' : `${snapshot.size_gb} GB`}</span>
              <span>State: {snapshot.state}</span>
            </li>)}
          </ol>}
  </section>;
}
