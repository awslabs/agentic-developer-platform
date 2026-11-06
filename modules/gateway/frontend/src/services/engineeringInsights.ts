import { apiClient, buildQueryString } from './api';

export interface BusinessContextEvidence {
  schema_version: 'results/1.0'; view: 'business_context'; product_id: string; project_id: string;
  as_of: string; dataset_version: string; mapping_versions: string[]; coverage: 'complete';
  work_count: number; cost_by_currency: Record<string, string>;
  contexts: Array<{ subject_ref: { record_id: string; record_type: string; record_revision: string } }>;
  next_cursor: string | null;
}

export async function getBusinessContextEvidence(productId: string, projectId: string, asOf: string, cursor?: string, signal?: AbortSignal) {
  const value = await apiClient.get<BusinessContextEvidence>(`/engineering-insights/v1/projects/${encodeURIComponent(projectId)}/results${buildQueryString({ view: 'business_context', product_id: productId, as_of: asOf, limit: 25, cursor })}`, signal);
  if (value.schema_version !== 'results/1.0' || value.view !== 'business_context' || value.product_id !== productId || value.project_id !== projectId || value.as_of !== asOf || value.coverage !== 'complete' ||
    !Number.isInteger(value.work_count) || value.work_count < 0 || typeof value.dataset_version !== 'string' || !Array.isArray(value.mapping_versions) || !value.mapping_versions.every(item => typeof item === 'string') ||
    !Array.isArray(value.contexts) || !value.contexts.every(item => item?.subject_ref && ['record_id', 'record_type', 'record_revision'].every(key => typeof item.subject_ref[key as keyof typeof item.subject_ref] === 'string')) ||
    !value.cost_by_currency || typeof value.cost_by_currency !== 'object' || Array.isArray(value.cost_by_currency) || !Object.values(value.cost_by_currency).every(amount => typeof amount === 'string' && /^-?\d+(\.\d+)?$/.test(amount)) ||
    !(value.next_cursor === null || typeof value.next_cursor === 'string')) throw new Error('Evidence does not match the requested product, project or reporting snapshot.');
  return value;
}
