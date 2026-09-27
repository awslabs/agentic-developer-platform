import { apiClient } from './api';
import type { OrganizationServiceIdentity } from './organizationServiceIdentities';
export interface TaskLimits {
  max_duration_minutes: number;
  max_turns: number;
  max_output_tokens_per_turn: number;
  max_usd_per_task: string | number;
}
export interface TaskPolicy {
  tenant_id: string; canonical_principal_id: string; version: number;
  status: 'active' | 'disabled'; allowed_personas: string[]; allowed_tools: string[];
  task_scopes: string[]; model_policy_version: string; model_policy_versions?: Record<string, string>; limits: TaskLimits;
  updated_at?: string; updated_by?: string;
}
export interface TaskPolicyView {
  tenant_id: string; canonical_principal_id: string; policy: TaskPolicy | null;
  platform_limits: TaskLimits; platform_limit_setting: string;
  persona_tools: Record<string, string[]>; models: { persona: string; model: string | null; revision?: string | null }[];
}
export interface ReservationPreview {
  status: 'available' | 'unavailable'; reservation_usd?: string; reason?: string;
  max_input_tokens?: number; max_output_tokens?: number;
}
const base = (org: string, principal: string) => `/admin/organizations/${encodeURIComponent(org)}/task-policies/${encodeURIComponent(principal)}`;
export async function identityPolicy(org: string, identity: OrganizationServiceIdentity): Promise<TaskPolicyView> {
  const query = new URLSearchParams({ source: identity.source, identity_id: identity.id });
  const resolved = await apiClient.get<{ tenant_id: string; canonical_principal_id: string }>(`/admin/organizations/${encodeURIComponent(org)}/task-policy-identity?${query}`);
  if (resolved.tenant_id !== org) throw new Error('Unexpected organization');
  const view = await apiClient.get<TaskPolicyView>(base(org, resolved.canonical_principal_id));
  if (view.tenant_id !== org || view.canonical_principal_id !== resolved.canonical_principal_id) throw new Error('Unexpected policy identity');
  return view;
}
export const getTaskPolicyView = (principal?: string) => apiClient.get<TaskPolicyView>(principal ? `/service-principals/${encodeURIComponent(principal)}/task-policy-view` : '/me/task-policy-view');
export const previewReservation = (model: string, output: number) => apiClient.get<ReservationPreview>(`/task-reservation-preview?${new URLSearchParams({ model, max_output_tokens: String(output) })}`);
export function saveTaskPolicy(org: string, principal: string, policy: TaskPolicy) {
  const { status, allowed_personas, allowed_tools, task_scopes, model_policy_version, model_policy_versions, limits } = policy;
  return apiClient.put<TaskPolicy>(base(org, principal), { expected_version: policy.version, status, allowed_personas, allowed_tools, task_scopes, model_policy_version, ...(model_policy_versions ? { model_policy_versions } : {}), limits });
}
