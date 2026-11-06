import { useEffect, useState } from 'react';
import { apiClient } from '@/services/api';

export interface CrewCheckpoint {
  id: string;
  status: string;
  sequence: number;
  captured_at: string | null;
  archive_size: number | null;
  failure_class: string | null;
  is_latest_verified: boolean;
}

interface BackupState {
  loading: boolean;
  unavailable: boolean;
  checkpoints: CrewCheckpoint[];
  failedCapture: CrewCheckpoint | null;
  selectedId: string | null;
}

export function useCrewCheckpoints(): BackupState {
  const [state, setState] = useState<BackupState>({ loading: true, unavailable: false,
    checkpoints: [], failedCapture: null, selectedId: null });

  useEffect(() => {
    let mounted = true;
    let requestId = 0;
    const refresh = async () => {
      const current = ++requestId;
      try {
        const listing = await apiClient.get<{ checkpoints: CrewCheckpoint[] }>('/v1/crew/checkpoints');
        let selectedId: string | null;
        try {
          const selection = await apiClient.get<{ checkpoint_id: string | null }>('/v1/crew/checkpoints/selection');
          selectedId = selection.checkpoint_id;
        } catch {
          selectedId = listing.checkpoints.find(row => row.is_latest_verified)?.id ?? null;
        }
        if (mounted && current === requestId) {
          const sorted = [...listing.checkpoints].sort((first, second) => second.sequence - first.sequence);
          setState({ loading: false, unavailable: false, checkpoints: sorted.filter(row => row.status === 'verified').slice(0, 5),
            failedCapture: sorted[0]?.status === 'failed' && sorted[0].failure_class === 'verification_failed' ? sorted[0] : null, selectedId });
        }
      } catch {
        if (mounted && current === requestId) setState({ loading: false, unavailable: true,
          checkpoints: [], failedCapture: null, selectedId: null });
      }
    };
    void refresh();
    window.addEventListener('focus', refresh);
    return () => { mounted = false; window.removeEventListener('focus', refresh); };
  }, []);

  return state;
}
