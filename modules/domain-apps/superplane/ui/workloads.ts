import { call, parseApprovalRequest, parseOperationState, type ScopeGuard } from './client';

export interface ServingInput {
  name: string;
  profile_id: string;
  model_name: string;
  precision: string;
  serving_framework: string;
  replicas: number;
  gpu_per_replica: number;
  tensor_parallel_size: number;
  max_model_len: number | null;
}

export interface ServingDeployment {
  deploymentId: string | null;
  operationId: string | null;
  operationState: ReturnType<typeof parseOperationState>;
  name: string;
  status: string;
  providerUid: string | null;
}

export interface ServingReview {
  deploymentId: string;
  requestId: string;
  revision: string;
  approvalRequest: Record<string, unknown>;
  account: string;
  region: string;
  namespace: string;
  image: string;
  resources: number;
  runtimeSeconds: number;
  maxCostMicros: number;
}

const record = (raw: unknown): raw is Record<string, unknown> =>
  typeof raw === 'object' && raw !== null && !Array.isArray(raw);
const id = (raw: unknown): raw is string => typeof raw === 'string' && raw.length > 0 && raw.length <= 255;
const text = (raw: unknown) => typeof raw === 'string' ? raw : null;

export function parseDeployment(raw: unknown): ServingDeployment | null {
  if (!record(raw) || !id(raw.name) || !id(raw.status)) return null;
  return {
    deploymentId: text(raw.deployment_id), operationId: text(raw.operation_id),
    operationState: parseOperationState(raw.operation_state), name: raw.name,
    status: raw.status, providerUid: text(raw.provider_uid),
  };
}

export function listDeployments(guard: ScopeGuard, workspaceId: string) {
  return call(guard, 'listDeployments', { workspace_id: workspaceId }, undefined, (raw) => {
    if (!record(raw) || raw.workspace_id !== workspaceId || !Array.isArray(raw.deployments)) return null;
    const rows = raw.deployments.map(parseDeployment);
    return rows.every((row): row is ServingDeployment => row !== null) ? rows : null;
  });
}

export function parseServingReview(raw: unknown, workspaceId: string, requestId: string,
  action: 'provision' | 'teardown', deploymentId?: string): ServingReview | null {
  if (!record(raw) || !id(raw.deployment_id) || raw.request_id !== requestId ||
      typeof raw.revision !== 'string' || !/^[a-f0-9]{64}$/.test(raw.revision) ||
      (deploymentId !== undefined && raw.deployment_id !== deploymentId)) return null;
  const approval = parseApprovalRequest(raw.approval_request);
  if (!approval || approval.workspace_id !== workspaceId || approval.idempotency_key !== requestId ||
      approval.action !== action || !record(approval.parameters)) return null;
  const parameters = approval.parameters;
  // Render the same plan sent for approval, never a separate display copy.
  if (parameters.controller_deployment_id !== raw.deployment_id || typeof parameters.controller_plan !== 'string') return null;
  let plan: unknown;
  try { plan = JSON.parse(parameters.controller_plan); } catch { return null; }
  if (!record(plan) || !record(plan.workload) || plan.workload.kind !== 'serving' ||
      !id(plan.provider_account_id) || !id(plan.region) || !id(plan.namespace) ||
      typeof plan.workload.image !== 'string' || !/^[a-zA-Z0-9./:_-]+@sha256:[a-f0-9]{64}$/.test(plan.workload.image)) return null;
  const values = ['max_resource_units', 'max_runtime_seconds', 'max_cost_micros'].map((key) => {
    const value = parameters[key];
    return typeof value === 'string' && /^\d+$/.test(value) ? Number(value) : NaN;
  });
  if (values.some((value) => !Number.isSafeInteger(value) || value < 0) || values[1] <= 0) return null;
  return {
    deploymentId: raw.deployment_id, requestId, revision: raw.revision, approvalRequest: approval,
    account: plan.provider_account_id, region: plan.region, namespace: plan.namespace,
    image: plan.workload.image, resources: values[0], runtimeSeconds: values[1], maxCostMicros: values[2],
  };
}

export function previewServing(guard: ScopeGuard, workspaceId: string, requestId: string,
  input: ServingInput | { deploymentId: string }) {
  const teardown = 'deploymentId' in input;
  return call(guard, teardown ? 'previewDeploymentTeardown' : 'previewDeployment',
    { workspace_id: workspaceId, ...(teardown ? { dep_id: input.deploymentId } : {}) },
    { ...(teardown ? {} : input), operation_id: requestId },
    (raw) => parseServingReview(raw, workspaceId, requestId, teardown ? 'teardown' : 'provision',
      teardown ? input.deploymentId : undefined));
}

export function submitServing(guard: ScopeGuard, workspaceId: string, review: ServingReview,
  approvalId: string, input: ServingInput | { deploymentId: string }) {
  const teardown = 'deploymentId' in input;
  return call(guard, teardown ? 'deleteDeployment' : 'createDeployment',
    { workspace_id: workspaceId, ...(teardown ? { dep_id: input.deploymentId } : {}) },
    { ...(teardown ? {} : input), operation_id: review.requestId, approval_id: approvalId, plan_revision: review.revision },
    (raw) => {
      const result = parseDeployment(raw);
      if (!result || !result.operationId || (!teardown && result.deploymentId !== review.deploymentId)) return null;
      return result;
    });
}
