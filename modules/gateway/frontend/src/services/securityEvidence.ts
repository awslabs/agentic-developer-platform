import { apiClient } from './api';
import type { SecurityConfiguration } from './l5Services';

export interface SecurityEvidenceSnapshot {
  configuration: SecurityConfiguration;
  binding: { binding_id: string; revision: number; [key: string]: unknown };
  run: { run_id: string; revision: number; [key: string]: unknown } | null;
}

export interface SecurityEvidenceRequest {
  release: { source_revision: string; flow_id: string; run_id: number };
  selection: { tenant_id: string; product_id: string; project_id: string; phase: 'readiness' | 'edit' | 'display';
    observer_id: string; configuration_id: string; configuration_revision: number; ui_region: string; ui_reason: string; ui_receipt: string };
  expires_at: string;
  export_only?: boolean;
}

export interface SecurityEvidenceView {
  observer_id?: string;
  request: SecurityEvidenceRequest;
  snapshot: SecurityEvidenceSnapshot;
  snapshot_sha256: string;
  maximum_expires_at?: string;
  challenge_id?: string;
  expires_at?: string;
}

const path = (productId: string, projectId: string, requestId: string) =>
  `/v1/products/${encodeURIComponent(productId)}/projects/${encodeURIComponent(projectId)}/services/security/release-evidence/requests/${encodeURIComponent(requestId)}`;

export const readSecurityEvidence = (productId: string, projectId: string, requestId: string) =>
  apiClient.request<{ status: string; next_action: string; request: SecurityEvidenceRequest }>(path(productId, projectId, requestId));

export const securityEvidenceAction = <T = SecurityEvidenceView>(productId: string, projectId: string, requestId: string,
  action: 'preview' | 'endorse' | 'prepare' | 'observe' | 'revoke' | 'reconcile', body?: unknown) =>
  apiClient.request<T>(`${path(productId, projectId, requestId)}/${action}`, { method: 'POST', ...(body ? { body } : {}) });
