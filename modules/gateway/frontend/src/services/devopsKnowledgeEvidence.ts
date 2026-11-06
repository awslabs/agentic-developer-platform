import { apiClient } from './api';
import { devOpsPath } from './l5Services';
import type { RecordReference } from './products';

export interface KnowledgeSelection {
  tenant_id: string; product_id: string; project_id: string; configuration_id: string;
  configuration_revision: number; binding_id: string; binding_generation: number;
}
export interface KnowledgePublication {
  source_key: string; source_ref: RecordReference; native_type: string;
  active: boolean; metadata: Record<string, unknown>; asset_id?: string; version?: number; source_expires_at?: string;
}
export interface KnowledgePreparation {
  source_key: string; flow_id: string; challenge_id: string; prompt: string;
  expected_behavior: string; source_excerpt: string; reason: string;
}
export interface AssetReadback {
  asset_id: string; version: number; observed_at: string; content: string; metadata: Record<string, unknown>;
}
export interface KnowledgeObservation {
  preparation_hash: string; native_session_id: string; native_agent_type: string; session_started_at: string;
  before: AssetReadback; after: AssetReadback; transcript: { role: 'user' | 'assistant'; text: string }[];
  response_quote: string; source_citation_quote: string; skill_invocation_quote?: string; explanation: string;
}
export interface KnowledgeEvidence {
  evidence_id: string; kind: string; actor_id: string; event_hash: string; created_at: string;
  expires_at?: string; source_key?: string; preparation_hash?: string;
  selection?: { source_key: string; receipt: { metadata: Record<string, unknown> }; native_type: string };
}
export interface KnowledgeEvidencePage {
  operation_id: string; state: string; revision: number; selection: KnowledgeSelection;
  publications: KnowledgePublication[]; items: KnowledgeEvidence[];
  eligibility: { can_prepare: boolean; can_review: boolean; blocked_code: string | null };
}
export interface KnowledgeOperation {
  operation_id: string; binding_id: string; state: string; revision: number;
  configuration_revision: number; binding_generation: number;
}
export const listKnowledgeOperations = (product: string, project: string, binding: string, offset = 0) =>
  apiClient.get<{ items: KnowledgeOperation[]; next_offset: number | null }>(`${devOpsPath(product, project)}/knowledge-operations?binding_id=${encodeURIComponent(binding)}&offset=${offset}&limit=50`);
export interface NativeArrival {
  flowId: string; challenge_id: string; service_id: string; subject: string; expires_at: string;
  observer_id?: string; snapshot: { selection: Partial<KnowledgeSelection> };
  observation_receipt?: { provenance?: string; actor_id?: string; observed?: { observation?: string } } | null;
}
export const knowledgeEvidencePath = (product: string, project: string, operation: string) =>
  `${devOpsPath(product, project)}/binding-operations/${encodeURIComponent(operation)}/knowledge-evidence`;
export const readKnowledgeEvidence = (product: string, project: string, operation: string) =>
  apiClient.requestWithEtag<KnowledgeEvidencePage>(knowledgeEvidencePath(product, project, operation));
export const readKnowledgeArtifact = <T>(path: string, evidence: string, kind: 'prepared' | 'observed') =>
  apiClient.get<{ evidence_id: string; kind: string; event_hash: string; artifact: T }>(`${path}/${encodeURIComponent(evidence)}/artifacts/${kind}`);

export function knowledgeError(error: unknown): string {
  const failure = error as { status?: number; detail?: { next_action?: unknown; code?: string } } | null;
  if (typeof failure?.detail?.next_action === 'string') return failure.detail.next_action;
  const guidance: Record<string, string> = {
    knowledge_evidence_source_access_revoked: 'Access to a selected source was revoked. Restore legitimate current access before reading or reviewing its evidence.',
    knowledge_evidence_source_expired: 'The retained source expired. Preserve this original operation and ask its owner to review the missing source evidence.',
    knowledge_evidence_publication_not_ready: 'The original publication is not ready for review. Check its binding and delivery status before continuing.',
    knowledge_evidence_expired: 'The observation window expired. Prepare a fresh scenario before a new manually initiated conversation.',
    knowledge_evidence_inactive_requires_non_use: 'This publication is inactive. Keep its evidence for the separate non-use review.',
  };
  if (failure?.detail?.code && guidance[failure.detail.code]) return guidance[failure.detail.code];
  if (failure?.status === 401) return 'Sign in again, then refresh the original operation before continuing.';
  if (failure?.status === 403 || failure?.status === 404) return 'Evidence access is unavailable for this selection. Restore your current Product access before continuing.';
  if (failure?.status === 412) return 'The operation changed. Refresh its evidence and review the current state before continuing.';
  if (failure?.status === 422) return 'Check the entered evidence, including timestamps, exact quotes and readback metadata, then submit again.';
  return 'The request was not confirmed. Refresh retained evidence before retrying the saved request. Keep the original native session and publication.';
}
