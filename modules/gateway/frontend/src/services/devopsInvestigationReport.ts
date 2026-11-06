import { apiClient } from './api';
import { devOpsPath, matchesSecurityScope, type DevOpsReportArtifact, type DevOpsResult, type SecurityScope } from './l5Services';

type Document = Record<string, unknown>;
export interface DevOpsReport {
  task: Document;
  journal: Document[];
  recommendations: Document[];
  evidence: Document;
}
export class DevOpsReportError extends Error {}
const record = (value: unknown): value is Document => typeof value === 'object' && value !== null && !Array.isArray(value);
const sameArtifact = (value: DevOpsReportArtifact | undefined, expected: DevOpsReportArtifact) =>
  value && (['artifact_id', 'version', 'sha256', 'media_type', 'bytes'] as const).every(field => value[field] === expected[field]);
const sameNative = (value: DevOpsResult['native_job_ref'], expected: DevOpsResult['native_job_ref']) =>
  value && (['tenant_id', 'project_id', 'source_connection_id', 'record_type', 'record_id', 'record_revision'] as const)
    .every(field => value[field] === expected[field]);

export async function readDevOpsInvestigationReport(productId: string, projectId: string, scope: SecurityScope,
  selected: DevOpsResult, signal: AbortSignal): Promise<DevOpsReport> {
  const scoped = (value: DevOpsResult) => matchesSecurityScope(value.scope, scope, productId, projectId)
    && value.service_id === 'devops';
  const reference = selected.evidence_ref;
  if (!scoped(selected) || !reference || !['adp_submitted', 'native'].includes(selected.origin ?? '')
    || reference.artifact_id !== `readback-${selected.result_id}` || reference.media_type !== 'application/json'
    || !reference.version || !/^[a-f0-9]{64}$/.test(reference.sha256) || !Number.isSafeInteger(reference.bytes)
    || reference.bytes < 1 || reference.bytes > 8 * 1024 * 1024) {
    throw new DevOpsReportError('The selected report has no valid retained evidence. Refresh DevOps results and select the report again.');
  }
  signal.throwIfAborted();
  const root = devOpsPath(productId, projectId);
  const current = await apiClient.requestWithEtag<DevOpsResult>(`${root}/results/${encodeURIComponent(selected.result_id)}`, { signal });
  signal.throwIfAborted();
  // CloudFront weakens ETags when compressing the JSON representation. This is
  // a read-only revision check; evidence still requires exact identity and SHA-256.
  // Do not normalize ETags used by write preconditions.
  if (!scoped(current.value) || current.value.result_id !== selected.result_id || current.value.run_id !== selected.run_id
    || current.value.scope.owner_id !== selected.scope.owner_id
    || current.value.revision !== selected.revision || !([`"${selected.revision}"`, `W/"${selected.revision}"`].includes(current.etag))
    || current.value.origin !== selected.origin || current.value.native_task_id !== selected.native_task_id
    || !sameNative(current.value.native_job_ref, selected.native_job_ref) || !sameArtifact(current.value.evidence_ref, reference)) {
    throw new DevOpsReportError('The selected result changed. Refresh DevOps results before reading its report.');
  }
  const artifactPath = `${root}/artifacts/${encodeURIComponent(reference.artifact_id)}`;
  const access = await apiClient.get<{ artifact: DevOpsReportArtifact; download_path: string; expires_at: string }>(artifactPath, signal);
  signal.throwIfAborted();
  if (!sameArtifact(access.artifact, reference) || access.download_path !== `${artifactPath}/content`
    || !(Date.parse(access.expires_at) > Date.now())) {
    throw new DevOpsReportError('The retained report reference changed or expired. Refresh DevOps results and read it again.');
  }
  // Construct the fixed scoped endpoint; never follow a URL from report content.
  const evidence = await apiClient.requestVerifiedJson(`${artifactPath}/content`, reference, signal);
  signal.throwIfAborted();
  if (!record(evidence)) throw new DevOpsReportError('The retained report is malformed. Ask the platform administrator to inspect this result.');
  const investigation = selected.origin === 'adp_submitted';
  const task = investigation ? evidence.task : evidence;
  if (!record(task) || task.executionId !== selected.native_job_ref.record_id || typeof task.taskId !== 'string'
    || !task.taskId || (selected.native_task_id && task.taskId !== selected.native_task_id)
    || (investigation && (evidence.kind !== 'devops_investigation'
      || !sameNative(evidence.native_job_ref as DevOpsResult['native_job_ref'], selected.native_job_ref)))) {
    throw new DevOpsReportError('The retained report does not match this investigation. Refresh results; contact the platform administrator if the mismatch persists.');
  }
  const journal = investigation ? evidence.journal_records : [];
  const recommendations = investigation ? evidence.operational_recommendations : [];
  if (!Array.isArray(journal) || !journal.every(record) || !Array.isArray(recommendations) || !recommendations.every(record)) {
    throw new DevOpsReportError('The retained report is incomplete. Ask the platform administrator to inspect this result.');
  }
  return { task, journal, recommendations, evidence };
}
