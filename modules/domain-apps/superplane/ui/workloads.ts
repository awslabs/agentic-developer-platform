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

export interface BatchOptions {
  image: string;
  command: string[];
  args: string[];
  gpu_count: number;
  cpu: string;
  memory: string;
}
export interface BatchInput { name: string; profile_id: string; batch_options: BatchOptions }
export type WorkloadInput = ServingInput | BatchInput | { deploymentId: string };
export type WorkloadKind = 'serving' | 'batch';
export interface BatchProfile { profileId: string; options: BatchOptions }
export interface BatchCatalog { profiles: BatchProfile[]; canSubmit: boolean; canReviewTeardown: boolean; canCancel: boolean }

export interface ServingDeployment {
  deploymentId: string | null;
  operationId: string | null;
  operationState: ReturnType<typeof parseOperationState> | 'cancelled';
  name: string;
  status: string;
  providerUid: string | null;
  cancellationRequested: boolean;
  cleanupStatus: 'confirmed' | 'unconfirmed' | 'not-required';
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
  batchOptions?: BatchOptions;
}

export interface ServingProfile {
  profileId: string;
  modelOptions: Omit<ServingInput, 'name' | 'profile_id'>;
  image: string;
}
export interface ServingCatalog {
  profiles: ServingProfile[];
  canSubmit: boolean;
  canReviewTeardown: boolean;
  canCancel: boolean;
}

const record = (raw: unknown): raw is Record<string, unknown> =>
  typeof raw === 'object' && raw !== null && !Array.isArray(raw);
const id = (raw: unknown): raw is string => typeof raw === 'string' && raw.length > 0 && raw.length <= 255;
const text = (raw: unknown) => typeof raw === 'string' ? raw : null;

export function parseBatchOptions(raw: unknown): BatchOptions | null {
  if (!record(raw) || typeof raw.image !== 'string' || raw.image.length > 512 || !/^[a-zA-Z0-9./:_-]+@sha256:[a-f0-9]{64}$/.test(raw.image) ||
      !Array.isArray(raw.command) || raw.command.length === 0 || raw.command.length > 32 ||
      !Array.isArray(raw.args) || raw.args.length > 32 ||
      [...raw.command, ...raw.args].some((arg) => typeof arg !== 'string' || arg.length > 1024) ||
      typeof raw.gpu_count !== 'number' || !Number.isInteger(raw.gpu_count) || raw.gpu_count < 1 || raw.gpu_count > 8 ||
      typeof raw.cpu !== 'string' || !/^[1-9][0-9]{0,4}m?$/.test(raw.cpu) ||
      typeof raw.memory !== 'string' || !/^[1-9][0-9]{0,4}[MG]i$/.test(raw.memory)) return null;
  return { image: raw.image, command: [...raw.command] as string[], args: [...raw.args] as string[], gpu_count: raw.gpu_count, cpu: raw.cpu, memory: raw.memory };
}

export function getBatchCatalog(guard: ScopeGuard, workspaceId: string) {
  return call(guard, 'batchProfiles', { workspace_id: workspaceId }, undefined, (raw): BatchCatalog | null => {
    if (!record(raw) || raw.workspace_id !== workspaceId || !Array.isArray(raw.profiles) || raw.profiles.length > 128 ||
        typeof raw.can_submit !== 'boolean' || typeof raw.can_review_teardown !== 'boolean') return null;
    const profiles: BatchProfile[] = [];
    for (const entry of raw.profiles) {
      if (!record(entry) || typeof entry.profile_id !== 'string' || !/^[a-z][a-z0-9-]{0,62}$/.test(entry.profile_id)) return null;
      const options = parseBatchOptions(entry.batch_options);
      if (!options || options.image !== entry.image) return null;
      profiles.push({ profileId: entry.profile_id, options });
    }
    return { profiles, canSubmit: raw.can_submit && profiles.length > 0, canReviewTeardown: raw.can_review_teardown, canCancel: raw.can_cancel === true };
  });
}

export function listBatchJobs(guard: ScopeGuard, workspaceId: string) {
  return call(guard, 'listBatchJobs', { workspace_id: workspaceId }, undefined, (raw) => {
    if (!record(raw) || raw.workspace_id !== workspaceId || !Array.isArray(raw.jobs) || raw.jobs.length > 100 || typeof raw.truncated !== 'boolean') return null;
    const jobs = raw.jobs.map((job): ServingDeployment | null => {
      if (!record(job) || !id(job.job_id)) return null;
      return parseDeployment({ ...job, deployment_id: job.job_id });
    });
    return jobs.every((job): job is ServingDeployment => job !== null) ? { jobs, truncated: raw.truncated } : null;
  });
}

export function getServingCatalog(guard: ScopeGuard, workspaceId: string) {
  return call(guard, 'servingProfiles', { workspace_id: workspaceId }, undefined, (raw): ServingCatalog | null => {
    if (!record(raw) || raw.workspace_id !== workspaceId || !Array.isArray(raw.profiles) || raw.profiles.length > 128 ||
        typeof raw.can_submit !== 'boolean' || typeof raw.can_review_teardown !== 'boolean') return null;
    const profiles: ServingProfile[] = [];
    for (const entry of raw.profiles) {
      if (!record(entry) || typeof entry.profile_id !== 'string' || !/^[a-z][a-z0-9-]{0,62}$/.test(entry.profile_id) ||
          typeof entry.image !== 'string' || !/^[a-zA-Z0-9./:_-]+@sha256:[a-f0-9]{64}$/.test(entry.image) || !record(entry.model_options)) return null;
      const model = entry.model_options;
      if (typeof model.model_name !== 'string' || model.model_name.length === 0 || model.model_name.length > 500 ||
          typeof model.precision !== 'string' || !['fp16', 'bf16', 'fp8', 'awq', 'int8'].includes(model.precision) ||
          typeof model.serving_framework !== 'string' || !['vllm', 'sglang'].includes(model.serving_framework) || model.replicas !== 1 ||
          typeof model.gpu_per_replica !== 'number' || !Number.isInteger(model.gpu_per_replica) || model.gpu_per_replica < 1 || model.gpu_per_replica > 8 ||
          typeof model.tensor_parallel_size !== 'number' || !Number.isInteger(model.tensor_parallel_size) || model.tensor_parallel_size < 1 || model.tensor_parallel_size > 8 ||
          (model.max_model_len !== null && (typeof model.max_model_len !== 'number' || !Number.isInteger(model.max_model_len) || model.max_model_len < 256 || model.max_model_len > 1048576))) return null;
      profiles.push({ profileId: entry.profile_id, image: entry.image, modelOptions: {
        model_name: model.model_name, precision: model.precision, serving_framework: model.serving_framework,
        replicas: 1, gpu_per_replica: model.gpu_per_replica, tensor_parallel_size: model.tensor_parallel_size, max_model_len: model.max_model_len,
      } });
    }
    return { profiles, canSubmit: raw.can_submit && profiles.length > 0, canReviewTeardown: raw.can_review_teardown, canCancel: raw.can_cancel === true };
  });
}

export function parseDeployment(raw: unknown): ServingDeployment | null {
  if (!record(raw) || !id(raw.name) || !id(raw.status)) return null;
  return {
    deploymentId: text(raw.deployment_id), operationId: text(raw.operation_id),
    operationState: raw.operation_state === 'cancelled' ? 'cancelled' : parseOperationState(raw.operation_state), name: raw.name,
    status: raw.status, providerUid: text(raw.provider_uid),
    cancellationRequested: raw.cancellation_requested === true,
    cleanupStatus: raw.cleanup_status === 'confirmed' || raw.cleanup_status === 'not-required' ? raw.cleanup_status : 'unconfirmed',
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
  action: 'provision' | 'teardown', deploymentId?: string, kind: WorkloadKind = 'serving'): ServingReview | null {
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
  if (!record(plan) || !record(plan.workload) || plan.workload.kind !== kind ||
      !id(plan.provider_account_id) || !id(plan.region) || !id(plan.namespace) ||
      typeof plan.workload.image !== 'string' || !/^[a-zA-Z0-9./:_-]+@sha256:[a-f0-9]{64}$/.test(plan.workload.image)) return null;
  const values = ['max_resource_units', 'max_runtime_seconds', 'max_cost_micros'].map((key) => {
    const value = parameters[key];
    return typeof value === 'string' && /^\d+$/.test(value) ? Number(value) : NaN;
  });
  if (values.some((value) => !Number.isSafeInteger(value) || value < 0) || values[1] <= 0) return null;
  const batchOptions = kind === 'batch' ? parseBatchOptions(plan.workload) : undefined;
  if (kind === 'batch' && (!batchOptions || plan.workload.port !== null || plan.workload.auth_secret !== null || raw.job_id !== raw.deployment_id)) return null;
  return {
    deploymentId: raw.deployment_id, requestId, revision: raw.revision, approvalRequest: approval,
    account: plan.provider_account_id, region: plan.region, namespace: plan.namespace,
    image: plan.workload.image, resources: values[0], runtimeSeconds: values[1], maxCostMicros: values[2],
    ...(batchOptions ? { batchOptions } : {}),
  };
}

export function previewServing(guard: ScopeGuard, workspaceId: string, requestId: string,
  input: WorkloadInput, kind: WorkloadKind = 'serving') {
  const teardown = 'deploymentId' in input;
  return call(guard, kind === 'batch' ? (teardown ? 'previewBatchTeardown' : 'previewBatchJob') : (teardown ? 'previewDeploymentTeardown' : 'previewDeployment'),
    { workspace_id: workspaceId, ...(teardown ? { [kind === 'batch' ? 'job_id' : 'dep_id']: input.deploymentId } : {}) },
    { ...(teardown ? {} : input), operation_id: requestId },
    (raw) => parseServingReview(raw, workspaceId, requestId, teardown ? 'teardown' : 'provision',
      teardown ? input.deploymentId : undefined, kind));
}

export function submitServing(guard: ScopeGuard, workspaceId: string, review: ServingReview,
  approvalId: string, input: WorkloadInput, kind: WorkloadKind = 'serving') {
  const teardown = 'deploymentId' in input;
  return call(guard, kind === 'batch' ? (teardown ? 'deleteBatchJob' : 'createBatchJob') : (teardown ? 'deleteDeployment' : 'createDeployment'),
    { workspace_id: workspaceId, ...(teardown ? { [kind === 'batch' ? 'job_id' : 'dep_id']: input.deploymentId } : {}) },
    { ...(teardown ? {} : input), operation_id: review.requestId, approval_id: approvalId, plan_revision: review.revision },
    (raw) => {
      const result = parseDeployment(kind === 'batch' && record(raw) ? { ...raw, deployment_id: raw.job_id } : raw);
      if (kind === 'batch' && result?.deploymentId !== review.deploymentId) return null;
      if (!result || !result.operationId || (!teardown && result.deploymentId !== review.deploymentId)) return null;
      return result;
    });
}


export function cancelWorkload(guard: ScopeGuard, workspaceId: string, deploymentId: string, operationId: string, kind: WorkloadKind) {
  return call(guard, kind === 'batch' ? 'cancelBatchJob' : 'cancelDeployment',
    { workspace_id: workspaceId, [kind === 'batch' ? 'job_id' : 'dep_id']: deploymentId },
    { operation_id: operationId }, (raw) => {
      if (!record(raw) || raw.workspace_id !== workspaceId || raw.deployment_id !== deploymentId ||
          raw.operation_id !== operationId || typeof raw.cancellation_requested !== 'boolean' ||
          (kind === 'batch' && raw.job_id !== deploymentId) ||
          !['confirmed', 'unconfirmed', 'not-required'].includes(String(raw.cleanup_status)) ||
          (raw.cleanup_status === 'not-required' && (!raw.cancellation_requested || raw.operation_state !== 'cancelled'))) return null;
      return { requested: raw.cancellation_requested, state: parseOperationState(raw.operation_state), cleanup: String(raw.cleanup_status) };
    });
}
