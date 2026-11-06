import { apiClient } from './api';

export interface InstallationStatus {
  installation_id: string;
  status: 'partial' | 'unavailable';
  observed_at?: string;
  capabilities?: { installed_record?: { status: string } };
}

export function getInstallationStatus(installationId: string, signal?: AbortSignal): Promise<InstallationStatus> {
  return apiClient.get(`/assistant/installations/${encodeURIComponent(installationId)}/status`, signal);
}
