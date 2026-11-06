import { apiClient } from './api';
import { productReceipt, type RecordReference } from './products';

export interface SecurityOptions {
  connections: RecordReference[];
  sources: RecordReference[];
  regions: string[];
  capabilities: ('threat_model' | 'code_review')[];
  unavailable_reasons: string[];
}

export interface SecuritySettings {
  limits?: SecurityLimits;
  purpose?: string;
  classification?: string;
  profile_ref: RecordReference;
  source_refs: RecordReference[];
  connection_ref?: RecordReference;
  region?: string;
  capabilities: ('threat_model' | 'code_review')[];
  enabled: boolean;
  visibility: 'only_me' | 'product_team';
  exclusions: string[];
  overrides: { field: string; reason: string; value: unknown }[];
  triggers: { trigger_id: string; kind: string; enabled: boolean; capability?: string; binding_id?: string; source_refs: RecordReference[]; debounce_seconds: number; schedule?: string }[];
  monitoring?: { poll_interval_seconds?: number; poll_grace_seconds?: number; stall_after_seconds?: number };
}

export interface SecurityLimits {
  deadline_seconds: number;
  max_provider_attempts: number;
  max_polls: number;
  concurrency: number;
  max_cost: string;
  currency: 'USD';
  policy_ref: RecordReference;
}

export interface SecurityScope { tenant_id: string; product_id: string; project_id: string; relationship_revision: string; owner_id?: string }

export const matchesSecurityScope = (scope: SecurityScope | undefined, expected: SecurityScope | undefined, productId: string, projectId: string) =>
  Boolean(scope && expected && scope.tenant_id === expected.tenant_id && scope.product_id === productId && scope.project_id === projectId
    && scope.relationship_revision === expected.relationship_revision);

export interface SecurityConfiguration {
  scope: SecurityScope;
  configuration_id: string;
  revision: number;
  state: 'draft' | 'needs_input' | 'ready' | 'disabled';
  settings: SecuritySettings;
  inherited_fields: string[];
  gaps: string[];
  handoff_state: string;
  hash: string;
}

export interface Versioned<T> { value: T; etag: string }

export interface WellArchitectedSettings {
  review_mode: 'source_only';
  pillars: ('COST_OPTIMIZATION' | 'SECURITY' | 'RESILIENCE' | 'PERFORMANCE' | 'OPERATIONAL_EXCELLENCE')[];
  goal_refs: RecordReference[];
  execution_role_ref?: RecordReference;
  source_store_ref?: RecordReference;
}
export interface WellArchitectedOptions extends Omit<SecurityOptions, 'capabilities'> {
  capabilities: 'architecture_review'[];
}
export type WellArchitectedConfiguration = Omit<SecurityConfiguration, 'settings'> & {
  settings: Omit<SecuritySettings, 'capabilities'> & { service_id: 'well_architected'; capabilities: 'architecture_review'[];
    well_architected: WellArchitectedSettings; purpose?: string; classification?: string };
};
export const wellArchitectedPath = (productId: string, projectId: string) =>
  `/v1/products/${encodeURIComponent(productId)}/projects/${encodeURIComponent(projectId)}/services/well_architected`;
export const getWellArchitectedOptions = (productId: string, projectId: string) =>
  apiClient.get<WellArchitectedOptions>(`${wellArchitectedPath(productId, projectId)}/configuration/options`);
export const getWellArchitectedConfiguration = (productId: string, projectId: string) =>
  apiClient.requestWithEtag<WellArchitectedConfiguration>(`${wellArchitectedPath(productId, projectId)}/configuration`);
export interface WellArchitectedProviderInput {
  connection_ref: RecordReference;
  region: string;
  execution_role_arn: string;
  source_bucket: string;
}
export interface WellArchitectedProviderSelection {
  provider_contacted: true;
  native_qualified: false;
  execution_role_ref: RecordReference;
  source_store_ref: RecordReference;
}
export const validateWellArchitectedProviderSelection = (productId: string, projectId: string, selection: WellArchitectedProviderInput) =>
  apiClient.post<WellArchitectedProviderSelection>(`${wellArchitectedPath(productId, projectId)}/configuration/provider-selection`, selection);
export const checkWellArchitectedConfiguration = (productId: string, projectId: string) =>
  apiClient.post<{ ready: boolean; gaps: string[] }>(`${wellArchitectedPath(productId, projectId)}/configuration/check`);
export const saveWellArchitectedConfiguration = (
  productId: string, projectId: string, choices: { service_id: 'well_architected'; capabilities: ['architecture_review']; enabled: boolean;
    source_refs?: RecordReference[]; connection_ref?: RecordReference; region?: string; classification?: string; limits?: SecurityLimits;
    well_architected: WellArchitectedSettings; overrides?: SecuritySettings['overrides']; triggers?: SecuritySettings['triggers'] },
  etag?: string, receipt = productReceipt(),
) => apiClient.requestWithEtag<WellArchitectedConfiguration>(`${wellArchitectedPath(productId, projectId)}/configuration`, {
  method: 'PUT', headers: { 'Idempotency-Key': receipt, ...(etag ? { 'If-Match': etag } : { 'If-None-Match': '*' }) }, body: choices,
});

export const listWellArchitectedBindings = (productId: string, projectId: string) => apiClient.get<{ items: SecurityBinding[] }>(`${wellArchitectedPath(productId, projectId)}/bindings`);

export interface TransformOptions extends Omit<SecurityOptions, 'capabilities'> {
  service_id: 'transform';
  capabilities: ('refactor' | 'dependency_upgrade')[];
  readiness: { capability: string; status: string; gaps: { code: string; field: string }[] }[];
}
export interface TransformSettings {
  connection_ref: RecordReference;
  executor_binding_ref?: RecordReference;
  test_policy_ref?: RecordReference;
  definition_ref?: RecordReference;
  region: string;
  read_paths: string[];
  write_paths: string[];
  excluded_paths: string[];
  goal_defaults: { kind: 'refactor' | 'dependency_upgrade'; summary: string; target?: unknown };
}
export type TransformConfiguration = Omit<SecurityConfiguration, 'settings'> & {
  settings: Omit<SecuritySettings, 'capabilities'> & {
    service_id: 'transform';
    capabilities: TransformOptions['capabilities'];
    transform: TransformSettings;
  };
};
export const transformPath = (productId: string, projectId: string) =>
  `/v1/products/${encodeURIComponent(productId)}/projects/${encodeURIComponent(projectId)}/services/transform`;
export const getTransformOptions = (productId: string, projectId: string) =>
  apiClient.get<TransformOptions>(`${transformPath(productId, projectId)}/configuration/options`);
export const getTransformConfiguration = (productId: string, projectId: string) =>
  apiClient.requestWithEtag<TransformConfiguration>(`${transformPath(productId, projectId)}/configuration`);
export const checkTransformConfiguration = (productId: string, projectId: string) =>
  apiClient.post<{ ready: boolean; gaps: string[] }>(`${transformPath(productId, projectId)}/configuration/check`);
export const getTransformConfigurationVersion = (productId: string, projectId: string, revision: number) =>
  apiClient.requestWithEtag<TransformConfiguration>(`${transformPath(productId, projectId)}/configuration/versions/${revision}`);
export const saveTransformConfiguration = (
  productId: string, projectId: string, choices: { service_id: 'transform'; capabilities: TransformOptions['capabilities'];
    enabled: boolean; transform: TransformSettings; source_refs?: RecordReference[]; overrides?: SecuritySettings['overrides'] },
  etag?: string, receipt = productReceipt(),
) => apiClient.requestWithEtag<TransformConfiguration>(`${transformPath(productId, projectId)}/configuration`, {
  method: 'PUT', headers: { 'Idempotency-Key': receipt, ...(etag ? { 'If-Match': etag } : { 'If-None-Match': '*' }) }, body: choices,
});

export interface TransformRunInput {
  service_id: 'transform'; configuration_revision: number; binding_id: string;
  capability: TransformOptions['capabilities'][number];
  sources: RecordReference[]; transform_input: { goal: TransformSettings['goal_defaults'] };
}
export type TransformRun = SecurityRun & { input?: TransformRunInput };
export interface TransformCheck { name?: string; status: string; reason?: string; receipt?: { [key: string]: unknown } }
export interface TransformArtifact { artifact_id: string; version: string; sha256: string; media_type: string; bytes: number; tenant_id: string; project_id: string; expires_at: string }
export interface TransformResult {
  service_id: 'transform'; scope: SecurityScope; result_id: string; run_id: string; revision: number;
  observed_at: string; verification_status: string; provider_attribution?: { [key: string]: unknown };
  original_patch: { sha256: string; bytes: number }; retained_artifacts: { original_patch?: TransformArtifact };
  tests_before: TransformCheck[]; tests_after: TransformCheck[];
  scope_status: string; dependency_comparison?: { status: string }; limitations: string[];
  cost_status: 'observed' | 'unknown'; retention_expires_at: string;
}
export const listTransformBindings = (productId: string, projectId: string) =>
  apiClient.get<{ items: SecurityBinding[] }>(`${transformPath(productId, projectId)}/bindings`);
export const listTransformRuns = (productId: string, projectId: string, cursor?: string) =>
  apiClient.get<{ items: TransformRun[]; next_cursor?: string }>(`${transformPath(productId, projectId)}/runs${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''}`);
export const getTransformRun = (productId: string, projectId: string, runId: string) =>
  apiClient.requestWithEtag<TransformRun>(`${transformPath(productId, projectId)}/runs/${encodeURIComponent(runId)}`);
export const submitTransformRun = (productId: string, projectId: string, input: TransformRunInput, receipt = productReceipt()) =>
  apiClient.requestWithEtag<TransformRun>(`${transformPath(productId, projectId)}/runs`, {
    method: 'POST', headers: { 'Idempotency-Key': receipt }, body: input,
  });
export const mutateTransformRun = (productId: string, projectId: string, runId: string, action: 'cancel' | 'reconcile', reason: string, receipt = productReceipt()) =>
  apiClient.requestWithEtag<TransformRun>(`${transformPath(productId, projectId)}/runs/${encodeURIComponent(runId)}/${action}`, {
    method: 'POST', headers: { 'Idempotency-Key': receipt }, body: { reason },
  });
export const listTransformResults = (productId: string, projectId: string, cursor?: string) =>
  apiClient.get<{ items: TransformResult[]; next_cursor?: string }>(`${transformPath(productId, projectId)}/results${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''}`);
export const getTransformResult = (productId: string, projectId: string, resultId: string) =>
  apiClient.requestWithEtag<TransformResult>(`${transformPath(productId, projectId)}/results/${encodeURIComponent(resultId)}`);
export const getTransformArtifact = (productId: string, projectId: string, artifactId: string) =>
  apiClient.get<{ artifact: TransformArtifact; download_path: string; expires_at: string }>(`${transformPath(productId, projectId)}/artifacts/${encodeURIComponent(artifactId)}`);
export const readTransformPatch = (productId: string, projectId: string, artifactId: string, sha256: string) =>
  apiClient.requestVerifiedText(`${transformPath(productId, projectId)}/artifacts/${encodeURIComponent(artifactId)}/content`, sha256);

export type DevOpsCapability = 'investigation' | 'release_readiness' | 'release_verification' | 'prevention';
export interface DevOpsOptions {
  connections: RecordReference[];
  sources: RecordReference[];
  regions: string[];
  capabilities: DevOpsCapability[];
  unavailable_reasons: string[];
}
export interface DevOpsSettings {
  classification?: string | null;
  profile_ref: RecordReference;
  source_refs: RecordReference[];
  region?: string;
  capabilities: DevOpsCapability[];
  enabled: boolean;
  visibility: 'only_me' | 'product_team';
  exclusions: string[];
  overrides: { field: string; reason: string; value: string | RecordReference | RecordReference[] }[];
  triggers: [];
  service_id: 'devops';
  devops: {
    hosting_connection_ref: RecordReference;
    region: string;
    space_mode: 'create' | 'adopt';
    existing_space_id?: string;
    accounts: unknown[];
    integrations: unknown[];
    access_policy_ref?: RecordReference;
    authentication_mode: 'scoped_iam_federation';
    permitted_native_actions: string[];
    knowledge_policy: { source_refs: RecordReference[]; visibility: 'only_me' | 'product_team'; publish_results: boolean; refresh: 'manual' | 'on_change' };
    trigger_policies: unknown[];
    prevention_enabled: boolean;
    retention_days: number;
    browser_launch_policy: 'disabled' | 'scoped_session';
    [key: string]: unknown;
  };
}
export type DevOpsConfiguration = Omit<SecurityConfiguration, 'settings'> & { settings: DevOpsSettings };
export const devOpsPath = (productId: string, projectId: string) =>
  `/v1/products/${encodeURIComponent(productId)}/projects/${encodeURIComponent(projectId)}/services/devops`;
export const getDevOpsOptions = (productId: string, projectId: string) =>
  apiClient.get<DevOpsOptions>(`${devOpsPath(productId, projectId)}/configuration/options`);
export const getDevOpsConfiguration = (productId: string, projectId: string) =>
  apiClient.requestWithEtag<DevOpsConfiguration>(`${devOpsPath(productId, projectId)}/configuration`);
export const saveDevOpsConfiguration = (
  productId: string, projectId: string, choices: Partial<DevOpsSettings>, etag?: string, receipt = productReceipt(),
) => apiClient.requestWithEtag<DevOpsConfiguration>(`${devOpsPath(productId, projectId)}/configuration`, {
  method: 'PUT', headers: { 'Idempotency-Key': receipt, ...(etag ? { 'If-Match': etag } : { 'If-None-Match': '*' }) }, body: choices,
});

export type DevOpsBinding = Omit<SecurityBinding, 'readiness'> & {
  provider_identity?: { account_id: string; role_arn: string; region: string };
  assets?: { kind: string; status: string; reason?: string }[];
  readiness: (SecurityReadinessEntry & { configuration_revision?: number; binding_generation?: number; valid_until?: string })[];
};
export type DevOpsRun = Omit<SecurityRun, 'input'> & { input?: DevOpsRunInput; origin?: 'adp_submitted' | 'native'; native_job_ref?: RecordReference | null; admission?: SecurityRun['admission'] & { admitted_at?: string } };
export interface DevOpsReportArtifact { artifact_id: string; version: string; sha256: string; media_type: 'application/json'; bytes: number }
export type DevOpsResult = Omit<SecurityResult, 'provider_outcome' | 'native_job_ref'> & { native_job_ref: RecordReference; origin?: 'adp_submitted' | 'native'; service_id?: string; native_event_ref?: RecordReference; native_task_id?: string; evidence_ref?: DevOpsReportArtifact; provider_outcome?: 'completed' | 'failed' | 'stopped' | 'outcome_unknown' };
export interface DevOpsEvent { event_id: string; scope: SecurityScope; binding_id: string; origin: 'native_event'; state: string; received_at: string; result_ref?: RecordReference }
export interface DevOpsPrevention { binding_id: string; configuration_revision: number; status: 'ready' | 'blocked'; reason: string | null; desired: { enabled: boolean }; observed: unknown }

const devOpsBindingPath = (productId: string, projectId: string, bindingId: string) =>
  `${devOpsPath(productId, projectId)}/bindings/${encodeURIComponent(bindingId)}`;
const devOpsResultPath = (productId: string, projectId: string, resultId: string) =>
  `${devOpsPath(productId, projectId)}/results/${encodeURIComponent(resultId)}`;
export const listDevOpsBindings = (productId: string, projectId: string) =>
  apiClient.get<{ items: DevOpsBinding[] }>(`${devOpsPath(productId, projectId)}/bindings`);
export const getDevOpsBinding = (productId: string, projectId: string, bindingId: string) =>
  apiClient.requestWithEtag<DevOpsBinding>(devOpsBindingPath(productId, projectId, bindingId));
export const startDevOpsBindingOperation = (
  productId: string, projectId: string, request: BindingRequest, etag?: string, receipt = productReceipt(),
) => apiClient.requestWithEtag<SecurityBindingOperation>(`${devOpsPath(productId, projectId)}/binding-operations`, {
  method: 'POST', headers: { 'Idempotency-Key': receipt, ...(etag ? { 'If-Match': etag } : {}) }, body: request,
});
export const getDevOpsBindingOperation = (productId: string, projectId: string, operationId: string) =>
  apiClient.get<SecurityBindingOperation>(`${devOpsPath(productId, projectId)}/binding-operations/${encodeURIComponent(operationId)}`);
export const listDevOpsEvents = (productId: string, projectId: string, bindingId: string) =>
  apiClient.get<{ items: DevOpsEvent[] }>(`${devOpsBindingPath(productId, projectId, bindingId)}/events`);
export const getDevOpsPrevention = (productId: string, projectId: string, bindingId: string) =>
  apiClient.get<DevOpsPrevention>(`${devOpsBindingPath(productId, projectId, bindingId)}/prevention`);
export const configureDevOpsPrevention = (
  productId: string, projectId: string, bindingId: string, configurationRevision: number,
  action: 'enable' | 'pause' | 'resume', etag: string, receipt = productReceipt(),
) => apiClient.request<DevOpsPrevention>(`${devOpsBindingPath(productId, projectId, bindingId)}/prevention`, {
  method: 'POST', headers: { 'Idempotency-Key': receipt, 'If-Match': etag }, body: { configuration_revision: configurationRevision, action },
});
export const launchDevOpsBrowser = (productId: string, projectId: string, bindingId: string, configurationRevision: number, observationId?: string) =>
  apiClient.request<{ binding_id: string; launch_url: string; expires_at: string; observation_receipt_id?: string }>(`${devOpsBindingPath(productId, projectId, bindingId)}/browser-launch`, {
    method: 'POST', headers: observationId ? { 'X-ADP-Observation-ID': observationId } : {}, body: { configuration_revision: configurationRevision },
  });
export interface DevOpsRunInput {
  service_id: 'devops'; configuration_revision: number; binding_id: string;
  capability: 'investigation' | 'release_readiness' | 'release_verification';
  devops_input: { kind: string; [key: string]: unknown };
}
export const listDevOpsRuns = (productId: string, projectId: string, cursor?: string) =>
  apiClient.get<{ items: DevOpsRun[]; next_cursor?: string }>(`${devOpsPath(productId, projectId)}/runs${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''}`);
export const getDevOpsRun = (productId: string, projectId: string, runId: string) =>
  apiClient.requestWithEtag<DevOpsRun>(`${devOpsPath(productId, projectId)}/runs/${encodeURIComponent(runId)}`);
export const reconcileDevOpsRun = (productId: string, projectId: string, runId: string, receipt: string,
  recovery?: Pick<DevOpsRun, 'revision' | 'native_job_ref'>) =>
  apiClient.requestWithEtag<DevOpsRun>(`${devOpsPath(productId, projectId)}/runs/${encodeURIComponent(runId)}/reconcile`, {
    method: 'POST', headers: { 'Idempotency-Key': receipt },
    body: { reason: 'Read the existing investigation and collect its report without starting another investigation.',
      ...(recovery ? { authorize_readback: true, expected_revision: recovery.revision, native_job_ref: recovery.native_job_ref } : {}) },
  });
export const submitDevOpsRun = (productId: string, projectId: string, input: DevOpsRunInput, receipt = productReceipt(), observationId?: string) =>
  apiClient.requestWithEtag<DevOpsRun>(`${devOpsPath(productId, projectId)}/runs`, {
    method: 'POST', headers: { 'Idempotency-Key': receipt, ...(observationId ? { 'X-ADP-Observation-Effect': observationId, 'X-ADP-Observation-Operation': 'submit' } : {}) }, body: input,
  });
export const listDevOpsResults = (productId: string, projectId: string, cursor?: string) =>
  apiClient.get<{ items: DevOpsResult[]; next_cursor?: string }>(`${devOpsPath(productId, projectId)}/results${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''}`);
export const listDevOpsFindings = (productId: string, projectId: string, resultId: string, cursor?: string) =>
  apiClient.get<{ items: SecurityFinding[]; next_cursor?: string }>(`${devOpsResultPath(productId, projectId, resultId)}/findings${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''}`);
export const getDevOpsPublication = (productId: string, projectId: string, publicationId: string) =>
  apiClient.get<SecurityPublication>(`${devOpsPath(productId, projectId)}/publications/${encodeURIComponent(publicationId)}`);
export const retryDevOpsPublication = (productId: string, projectId: string, publicationId: string, reason: string, receipt = productReceipt()) =>
  apiClient.request<SecurityPublication>(`${devOpsPath(productId, projectId)}/publications/${encodeURIComponent(publicationId)}/retry`, {
    method: 'POST', headers: { 'Idempotency-Key': receipt }, body: { reason },
  });

export const securityPath = (productId: string, projectId: string) =>
  `/v1/products/${encodeURIComponent(productId)}/projects/${encodeURIComponent(projectId)}/services/security`;

export const getSecurityOptions = (productId: string, projectId: string) =>
  apiClient.get<SecurityOptions>(`${securityPath(productId, projectId)}/configuration/options`);

export const getSecurityConfiguration = (productId: string, projectId: string) =>
  apiClient.requestWithEtag<SecurityConfiguration>(`${securityPath(productId, projectId)}/configuration`);

export interface SourceCapturePreflightResult {
  configuration_id: string;
  configuration_revision: number;
  observed_at: string;
  status: 'not_applicable' | 'unknown' | 'exceeds_limits' | 'within_limits';
  limits: { max_bytes: number; max_files: number };
  advisory_only: true;
  content_captured: false;
  run_authority_checked: false;
  limitations: string[];
  combined_repositories: {
    metadata_coverage: 'complete' | 'partial';
    known_selected_files: number;
    known_selected_bytes: number;
    snapshot_archive_bytes: number;
    native_combined_archive_bytes: null;
    reasons: string[];
  };
  sources: {
    source_ref: RecordReference;
    status: 'unknown' | 'exceeds_limits' | 'within_limits';
    metadata_coverage: 'complete' | 'unknown';
    coverage_scope?: 'selected_paths' | 'whole_repository';
    selected_files?: number;
    selected_bytes?: number;
    archive_bytes?: number;
    excluded_entries?: number;
    exclusions?: string[];
    limits?: { max_bytes: number; max_files: number };
    reasons: string[];
    oversized_files?: { path: string; bytes: number }[];
    oversized_file_count?: number;
    oversized_files_truncated?: boolean;
  }[];
}

export const getSecuritySourcePreflight = (productId: string, projectId: string, configurationRevision: number) =>
  apiClient.get<SourceCapturePreflightResult>(`${securityPath(productId, projectId)}/configuration/source-preflight?configuration_revision=${configurationRevision}`);

export const refreshSecurityProfile = (productId: string, projectId: string, profileRef: RecordReference, etag: string, receipt: string) =>
  apiClient.requestWithEtag<SecurityConfiguration>(`${securityPath(productId, projectId)}/configuration/refresh`, {
    method: 'POST', headers: { 'Idempotency-Key': receipt, 'If-Match': etag }, body: { profile_ref: profileRef },
  });

export const saveSecurityConfiguration = (
  productId: string, projectId: string, settings: Partial<Omit<SecuritySettings, 'profile_ref'>>,
  etag?: string, receipt = productReceipt(), evidenceChallenge?: string,
) => apiClient.requestWithEtag<SecurityConfiguration>(`${securityPath(productId, projectId)}/configuration`, {
  method: 'PUT',
  headers: { 'Idempotency-Key': receipt, ...(etag ? { 'If-Match': etag } : { 'If-None-Match': '*' }),
    ...(evidenceChallenge ? { 'X-ADP-Security-Evidence': evidenceChallenge } : {}) },
  body: settings,
});

export interface SecurityBinding {
  scope: SecurityScope;
  binding_id: string;
  configuration_id: string;
  revision: number;
  generation: number;
  state: string;
  ownership: 'adp_created' | 'customer_linked';
  connection_ref: RecordReference;
  native_resource_ref?: RecordReference;
  gaps: string[];
  readiness: SecurityReadinessEntry[];
}

/** Security binding readiness is what the provider adapter observed, not a
 *  capability/state matrix: the source registration entry is
 *  {kind: 'code_review_source_registration', registration, observed_at} and the
 *  optional threat-model entry is {kind: 'threat_model' |
 *  'threat_model_outcome_unknown' | 'threat_model_selection_required',
 *  native_model_ref?, candidate_id?, observed_at?}. Other services report
 *  {capability, state, reason_codes}. Every field is therefore optional and must
 *  be rendered through securityReadinessSummary rather than assumed present. */
export interface SecurityReadinessEntry {
  kind?: string;
  observation?: Record<string, unknown>;
  registration?: { [key: string]: unknown };
  native_model_ref?: RecordReference;
  candidate_id?: string;
  observed_at?: string;
  capability?: string;
  state?: string;
  reason_codes?: string[];
}

const readinessKindLabels: { [kind: string]: string } = {
  code_review_source_registration: 'code review source registration',
  threat_model: 'threat model',
  threat_model_outcome_unknown: 'threat model outcome unknown',
  threat_model_selection_required: 'threat model selection required',
};

export const securityReadinessSummary = (readiness: SecurityReadinessEntry[] | null | undefined) =>
  (readiness ?? []).map(entry => {
    if (entry.capability !== undefined || entry.state !== undefined) {
      const reasons = entry.reason_codes?.length ? ` (${entry.reason_codes.join(', ')})` : '';
      return `${entry.capability ?? entry.kind ?? 'readiness'}: ${entry.state ?? 'unknown'}${reasons}`;
    }
    const label = entry.kind ? readinessKindLabels[entry.kind] ?? entry.kind : 'readiness entry';
    const detail = [
      entry.native_model_ref?.record_id ? `model ${entry.native_model_ref.record_id}` : '',
      entry.candidate_id ? `candidate ${entry.candidate_id}` : '',
      entry.observed_at ? `observed ${entry.observed_at}` : '',
      entry.reason_codes?.length ? entry.reason_codes.join(', ') : '',
    ].filter(Boolean).join(', ');
    return detail ? `${label} (${detail})` : label;
  }).join('; ');

export interface SecurityBindingOperation {
  operation_id: string;
  action: BindingAction;
  state: 'accepted' | 'running' | 'waiting' | 'succeeded' | 'blocked' | 'failed' | 'outcome_unknown' | 'cancelled' | 'abandoned';
  revision: number;
  gaps: string[];
  binding_id?: string;
}

export type BindingAction = 'create' | 'link' | 'check' | 'update' | 'suspend' | 'resume' | 'unlink' | 'cleanup';
export type BindingRequest = { action: BindingAction; configuration_revision: number; binding_id?: string; native_resource_ref?: RecordReference; native_model_ref?: RecordReference; owned_cleanup_confirmed?: true };

export const listSecurityBindings = (productId: string, projectId: string) =>
  apiClient.get<{ items: SecurityBinding[] }>(`${securityPath(productId, projectId)}/bindings`);

export const getSecurityBinding = (productId: string, projectId: string, bindingId: string) =>
  apiClient.requestWithEtag<SecurityBinding>(`${securityPath(productId, projectId)}/bindings/${encodeURIComponent(bindingId)}`);

export const startSecurityBindingOperation = (
  productId: string, projectId: string, request: BindingRequest, etag?: string, receipt = productReceipt(),
) => apiClient.requestWithEtag<SecurityBindingOperation>(`${securityPath(productId, projectId)}/binding-operations`, {
  method: 'POST', headers: { 'Idempotency-Key': receipt, ...(etag ? { 'If-Match': etag } : {}) }, body: request,
});

export const getSecurityBindingOperation = (productId: string, projectId: string, operationId: string) =>
  apiClient.get<SecurityBindingOperation>(`${securityPath(productId, projectId)}/binding-operations/${encodeURIComponent(operationId)}`);

export const reconcileSecurityBindingOperation = (
  productId: string, projectId: string, operationId: string, reason: string, receipt = productReceipt(),
) => apiClient.requestWithEtag<SecurityBindingOperation>(`${securityPath(productId, projectId)}/binding-operations/${encodeURIComponent(operationId)}/reconcile`, {
  method: 'POST', headers: { 'Idempotency-Key': receipt }, body: { reason },
});

export interface SecurityRunInput {
  configuration_revision: number;
  binding_id: string;
  capability: SecuritySettings['capabilities'][number];
  sources: RecordReference[];
  pr_base?: string;
  pr_head?: string;
}

export interface SecurityRunMonitoring {
  state: 'not_started' | 'current' | 'overdue' | 'degraded' | 'finished' | 'recovery_pending';
  progress_state: 'unknown' | 'observed' | 'suspected_stalled';
  poll_attempts: number;
  consecutive_poll_errors: number;
  last_poll_attempt_at?: string;
  last_poll_error?: { code: string; stage?: string; cause?: string; message?: string; action?: string; next_action?: string };
  last_successful_poll_at?: string;
  next_poll_at?: string;
  last_progress_at?: string;
  progress_baseline_at?: string;
  latest_observation?: {
    status?: string;
    native_status?: string;
    outcome: string;
    progress_coverage: 'complete' | 'partial' | 'unavailable';
    phases?: { name: string; status?: string; native_status?: string }[];
    tasks?: { task_id: string; status: string; created_at?: string; updated_at?: string }[]
      | { running: number; completed: number; failed: number; stopped: number; unknown: number };
  };
}

export interface SecurityRun {
  scope: SecurityScope;
  run_id: string;
  revision: number;
  state: string;
  provider_state: string;
  deadline_at: string;
  input?: SecurityRunInput;
  monitoring?: SecurityRunMonitoring;
  admission?: { monitoring_policy: { poll_interval_seconds: number; poll_grace_seconds: number; stall_after_seconds?: number } };
  publication_id?: string;
}

export const listSecurityRuns = (productId: string, projectId: string, cursor?: string) =>
  apiClient.get<{ items: SecurityRun[]; next_cursor?: string }>(`${securityPath(productId, projectId)}/runs${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''}`);

export const getSecurityRun = (productId: string, projectId: string, runId: string) =>
  apiClient.requestWithEtag<SecurityRun>(`${securityPath(productId, projectId)}/runs/${encodeURIComponent(runId)}`);

export const submitSecurityRun = (
  productId: string, projectId: string, input: SecurityRunInput, receipt = productReceipt(), signal?: AbortSignal,
) => apiClient.requestWithEtag<SecurityRun>(`${securityPath(productId, projectId)}/runs`, {
  method: 'POST', headers: { 'Idempotency-Key': receipt }, body: input, ...(signal ? { signal } : {}),
});

export const mutateSecurityRun = (
  productId: string, projectId: string, runId: string, action: 'cancel' | 'reconcile' | 'retry', reason: string, receipt = productReceipt(), signal?: AbortSignal,
) => apiClient.requestWithEtag<SecurityRun>(`${securityPath(productId, projectId)}/runs/${encodeURIComponent(runId)}/${action}`, {
  method: 'POST', headers: { 'Idempotency-Key': receipt }, body: { reason }, ...(signal ? { signal } : {}),
});

export interface SecurityNativeJobReference {
  tenant_id: string;
  project_id: string;
  agent_space_id: string;
  artifact_id: string;
  threat_model_id: string;
  threat_model_job_id: string;
}

export interface SecurityResult {
  scope: SecurityScope;
  result_id: string;
  run_id: string;
  revision: number;
  observed_at: string;
  coverage: 'complete' | 'partial' | 'unknown';
  coverage_mode: string;
  finding_count?: number;
  provider_outcome?: 'completed' | 'failed' | 'outcome_unknown';
  native_job_ref: RecordReference | SecurityNativeJobReference;
  admission?: { adapter_revision: string; binding_generation: number; source_snapshots: { source_ref: RecordReference; snapshot_id: string }[] };
  publication_id?: string;
}

export type SecurityFindingLocation = string | { filePath: string; lineStart?: number; lineEnd?: number };

export interface SecurityFinding {
  finding_id: string;
  native_id: string;
  claim: string;
  kind: 'candidate_threat' | 'static_finding';
  provider_severity: string;
  validation: 'candidate' | 'supported' | 'contradicted' | 'unverified';
  source_refs: RecordReference[];
  evidence: { artifact_id: string; version: string }[];
  locations: SecurityFindingLocation[];
  assumptions: string[];
  mitigations: string[];
}

export interface SecurityTriage {
  finding_id: string;
  revision: number;
  actor_id: string;
  observed_at: string;
  decision: SecurityTriageInput;
}
export interface SecurityTriageInput { decision: 'open' | 'accepted_risk' | 'false_positive' | 'resolved'; reason: string }
export interface SecurityComparison {
  comparability: 'comparable' | 'partial' | 'incomparable';
  reasons: string[];
  baseline_revision?: number;
  candidate_revision?: number;
  new_findings?: string[];
  unchanged_findings?: string[];
  resolved_findings?: string[];
}
export interface SecurityPublication {
  publication_id: string;
  result_id: string;
  state: 'pending' | 'publishing' | 'published' | 'retry_wait' | 'unavailable' | 'invalidated';
  receipt_id?: string;
  consumer_revision?: string;
  invalidation_watermark: number;
}

const resultPath = (productId: string, projectId: string, resultId: string) =>
  `${securityPath(productId, projectId)}/results/${encodeURIComponent(resultId)}`;

export const listSecurityResults = (productId: string, projectId: string, cursor?: string) =>
  apiClient.get<{ items: SecurityResult[]; next_cursor?: string }>(`${securityPath(productId, projectId)}/results${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''}`);
export const getSecurityResult = (productId: string, projectId: string, resultId: string) =>
  apiClient.get<SecurityResult>(resultPath(productId, projectId, resultId));
export const listSecurityFindings = (productId: string, projectId: string, resultId: string, cursor?: string) =>
  apiClient.get<{ items: SecurityFinding[]; next_cursor?: string }>(`${resultPath(productId, projectId, resultId)}/findings${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''}`);
const triagePath = (productId: string, projectId: string, resultId: string, findingId: string) =>
  `${resultPath(productId, projectId, resultId)}/findings/${encodeURIComponent(findingId)}/triage`;
export const listSecurityTriage = (productId: string, projectId: string, resultId: string, findingId: string) =>
  apiClient.requestWithEtag<{ items: SecurityTriage[]; next_cursor?: string }>(triagePath(productId, projectId, resultId, findingId));
export const setSecurityTriage = (productId: string, projectId: string, resultId: string, findingId: string, input: SecurityTriageInput, etag: string, receipt = productReceipt()) =>
  apiClient.requestWithEtag<SecurityTriage>(triagePath(productId, projectId, resultId, findingId), {
    method: 'PUT', headers: { 'Idempotency-Key': receipt, 'If-Match': etag }, body: input,
  });
export const compareSecurityResults = (productId: string, projectId: string, baselineId: string, candidateId: string) =>
  apiClient.post<SecurityComparison>(`${securityPath(productId, projectId)}/comparisons`, { baseline_result_id: baselineId, candidate_result_id: candidateId });
export const getSecurityPublication = (productId: string, projectId: string, publicationId: string) =>
  apiClient.get<SecurityPublication>(`${securityPath(productId, projectId)}/publications/${encodeURIComponent(publicationId)}`);
export const retrySecurityPublication = (productId: string, projectId: string, publicationId: string, reason: string, receipt = productReceipt()) =>
  apiClient.request<SecurityPublication>(`${securityPath(productId, projectId)}/publications/${encodeURIComponent(publicationId)}/retry`, {
    method: 'POST', headers: { 'Idempotency-Key': receipt }, body: { reason },
  });

export type FinOpsConfiguration = Omit<SecurityConfiguration, 'settings'> & {
  settings: { service_id: 'finops'; enabled: boolean; finops: {
    agent_mode?: 'create' | 'adopt'; native_agent_ref?: string; native_agent_display_name?: string;
  }; [key: string]: unknown };
};
export type FinOpsBinding = SecurityBinding;
export type FinOpsRun = SecurityRun;
export interface FinOpsRunInput {
  service_id: 'finops'; capability: 'cost_inquiry'; configuration_revision: number; binding_id: string;
  question: string; query: {
    start: string; end: string; metric: 'UnblendedCost'; granularity: 'DAILY' | 'MONTHLY';
    group_by: ('SERVICE' | 'LINKED_ACCOUNT')[]; currency: string;
  };
}
type FinOpsArtifact = { artifact_id: string; version: string; sha256: string };
type FinOpsMeasure = { amount: string | null; currency: string | null; estimated: boolean | null;
  source_refreshed_at: string | null; freshness_reason: string | null; coverage: 'complete' | 'partial' | 'unavailable' };
export interface FinOpsResult {
  scope: SecurityScope;
  result_id: string;
  revision: number;
  kind: 'finops_cost_inquiry';
  run_id: string;
  query: FinOpsRunInput['query'];
  scope_digest: string;
  binding_generation: number;
  attribution: 'account' | 'product_verified' | 'partial' | 'unknown';
  attribution_evidence: FinOpsArtifact[];
  publication_id?: string;
  native: { status: 'answered' | 'refused' | 'failed' | 'outcome_unknown' | 'unavailable';
    conversation_id: string | null; turn_id: string | null; turn_locator?: string; answer_artifact: FinOpsArtifact | null;
    observed_at: string | null; measure: FinOpsMeasure };
  oracle: { collection_id: string; query_hash: string; observed_at: string | null;
    measure: FinOpsMeasure; pages: FinOpsArtifact[] };
  comparison: { state: 'exact' | 'rounded' | 'mismatch' | 'incomparable' | 'unavailable';
    delta: string | null; display_quantum: string | null; reason: string | null };
  billing_scope: { account_ids: string[]; view_ref: string; snapshot_artifact: FinOpsArtifact };
}
export const finOpsPath = (productId: string, projectId: string) =>
  `/v1/products/${encodeURIComponent(productId)}/projects/${encodeURIComponent(projectId)}/services/finops`;
export const getFinOpsOptions = (productId: string, projectId: string) =>
  apiClient.get<{ unavailable_reasons: string[]; connections?: RecordReference[]; regions?: string[]; capabilities?: string[];
    admissions_enabled?: boolean;
    readiness?: { capability: string; status: string; gaps: string[] } }>(`${finOpsPath(productId, projectId)}/configuration/options`);
export const getFinOpsConfiguration = (productId: string, projectId: string) =>
  apiClient.requestWithEtag<FinOpsConfiguration>(`${finOpsPath(productId, projectId)}/configuration`);
export const saveFinOpsConfiguration = (productId: string, projectId: string, choices: Record<string, unknown>, etag?: string, receipt = productReceipt()) =>
  apiClient.requestWithEtag<FinOpsConfiguration>(`${finOpsPath(productId, projectId)}/configuration`, {
    method: 'PUT', headers: { 'Idempotency-Key': receipt, ...(etag ? { 'If-Match': etag } : { 'If-None-Match': '*' }) }, body: choices,
  });
export const listFinOpsBindings = (productId: string, projectId: string) =>
  apiClient.get<{ items: FinOpsBinding[] }>(`${finOpsPath(productId, projectId)}/bindings`);
export const startFinOpsBinding = (productId: string, projectId: string, request: BindingRequest, receipt = productReceipt()) =>
  apiClient.requestWithEtag<SecurityBindingOperation>(`${finOpsPath(productId, projectId)}/binding-operations`, {
    method: 'POST', headers: { 'Idempotency-Key': receipt }, body: request,
  });
export const getFinOpsBindingOperation = (productId: string, projectId: string, operationId: string) =>
  apiClient.get<SecurityBindingOperation>(`${finOpsPath(productId, projectId)}/binding-operations/${encodeURIComponent(operationId)}`);
export const listFinOpsRuns = (productId: string, projectId: string, cursor?: string) =>
  apiClient.get<{ items: FinOpsRun[]; next_cursor?: string }>(`${finOpsPath(productId, projectId)}/runs${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''}`);
export const getFinOpsRun = (productId: string, projectId: string, runId: string) =>
  apiClient.requestWithEtag<FinOpsRun>(`${finOpsPath(productId, projectId)}/runs/${encodeURIComponent(runId)}`);
export const submitFinOpsRun = (productId: string, projectId: string, input: FinOpsRunInput, receipt = productReceipt()) =>
  apiClient.requestWithEtag<FinOpsRun>(`${finOpsPath(productId, projectId)}/runs`, {
    method: 'POST', headers: { 'Idempotency-Key': receipt }, body: input,
  });
export const listFinOpsResults = (productId: string, projectId: string, cursor?: string) =>
  apiClient.get<{ items: FinOpsResult[]; next_cursor?: string }>(`${finOpsPath(productId, projectId)}/results${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''}`);
export const getFinOpsResult = (productId: string, projectId: string, resultId: string) =>
  apiClient.requestWithEtag<FinOpsResult>(`${finOpsPath(productId, projectId)}/results/${encodeURIComponent(resultId)}`);
export const getFinOpsNativeAnswer = (productId: string, projectId: string, resultId: string, revision: number) =>
  apiClient.get<{ result_id: string; run_id: string; revision: number; artifact: FinOpsArtifact; text: string }>(
    `${finOpsPath(productId, projectId)}/results/${encodeURIComponent(resultId)}/native-answer?revision=${revision}`);
export const getFinOpsPublication = (productId: string, projectId: string, publicationId: string) =>
  apiClient.get<SecurityPublication>(`${finOpsPath(productId, projectId)}/publications/${encodeURIComponent(publicationId)}`);
export const listWellArchitectedRuns = (productId: string, projectId: string, cursor?: string) =>
  apiClient.get<{ items: SecurityRun[]; next_cursor?: string }>(`${wellArchitectedPath(productId, projectId)}/runs${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''}`);
export const startWellArchitectedBinding = (productId: string, projectId: string, revision: number, receipt = productReceipt()) =>
  apiClient.requestWithEtag<SecurityBindingOperation>(`${wellArchitectedPath(productId, projectId)}/binding-operations`, {
    method: 'POST', headers: { 'Idempotency-Key': receipt }, body: { action: 'create', configuration_revision: revision },
  });
export const getWellArchitectedBindingOperation = (productId: string, projectId: string, operationId: string) =>
  apiClient.get<SecurityBindingOperation>(`${wellArchitectedPath(productId, projectId)}/binding-operations/${encodeURIComponent(operationId)}`);
export const getWellArchitectedRun = (productId: string, projectId: string, runId: string) =>
  apiClient.requestWithEtag<SecurityRun>(`${wellArchitectedPath(productId, projectId)}/runs/${encodeURIComponent(runId)}`);
export const submitWellArchitectedRun = (productId: string, projectId: string, bindingId: string, revision: number, sources: RecordReference[], receipt = productReceipt()) =>
  apiClient.requestWithEtag<SecurityRun>(`${wellArchitectedPath(productId, projectId)}/runs`, {
    method: 'POST', headers: { 'Idempotency-Key': receipt }, body: { service_id: 'well_architected', capability: 'architecture_review',
      configuration_revision: revision, binding_id: bindingId, sources },
  });
export type WellArchitectedResult = Omit<SecurityResult, 'coverage'> & {
  service_id?: 'well_architected'; review_mode?: 'source_only';
  coverage: string | Record<string, unknown>[]; pillars?: string[]; goals?: string[];
  limitations?: string[]; recommendations?: {
    native_id?: string; generation_id?: string; pillar?: string | null;
    title?: string | null; description?: string | null;
    comparison?: { status: string; explanation?: string };
  }[];
  comparison_state?: string; coverage_mode?: string;
};
export interface WellArchitectedPublicationStatus {
  result_id: string; revision: number;
  consumers: { destination: string; state: string; publication_id?: string; last_error?: string;
    projection_verified_on_ack: boolean; receipt_id?: string; consumer_revision?: string }[];
}
export const listWellArchitectedResults = (productId: string, projectId: string, cursor?: string) =>
  apiClient.get<{ items: WellArchitectedResult[]; next_cursor?: string }>(`${wellArchitectedPath(productId, projectId)}/results${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''}`);
export const getWellArchitectedResult = (productId: string, projectId: string, resultId: string) =>
  apiClient.requestWithEtag<WellArchitectedResult>(`${wellArchitectedPath(productId, projectId)}/results/${encodeURIComponent(resultId)}`);
export interface WellArchitectedComparison {
  baseline_result_id: string; candidate_result_id: string;
  comparability: 'comparable' | 'not_comparable'; reasons: string[];
}
export const compareWellArchitectedResults = (productId: string, projectId: string, baselineId: string, candidateId: string) =>
  apiClient.post<WellArchitectedComparison>(`${wellArchitectedPath(productId, projectId)}/comparisons`,
    { baseline_result_id: baselineId, candidate_result_id: candidateId });
export const getWellArchitectedPublicationStatus = (productId: string, projectId: string, resultId: string) =>
  apiClient.get<WellArchitectedPublicationStatus>(`${wellArchitectedPath(productId, projectId)}/results/${encodeURIComponent(resultId)}/publication-status`);
