import { createHash } from 'node:crypto';
import { z } from 'zod';
import { canonicalJson } from '../invocability-probe/canonical-json';

const owner = ['session_read', 'session_write', 'message_read', 'message_write', 'summary_read', 'summary_write',
  'artifact_read', 'artifact_write', 'memory_read', 'memory_write', 'cursor_read', 'object_read', 'direct_url_read'];
const cross = [...owner, 'forged_principal', 'forged_run', 'forged_session', 'forged_generation', 'forged_user_header', 'forged_tenant_header',
  'residue_file', 'residue_process'];
export const credentialProbes = ['environment', 'proc', 'projected_files', 'aws_provider', 'aws_cli', 'sts_identity',
  's3_read', 'dynamodb_read', 'secrets_read', 'metadata', 'kubernetes_api', 'network_routes'];
export const qualificationManifest = {
  version: 'e45-v1',
  probes: { a1: [...owner, 'sandbox_identity', ...credentialProbes],
    a2: [...cross, 'sandbox_identity', ...credentialProbes], b1: [...cross, 'sandbox_identity', ...credentialProbes] },
  limits: { duration_seconds: 120, probe_timeout_seconds: 5, result_bytes: 65536, model_turns: 0 },
};
export const qualificationDigest = createHash('sha256').update(canonicalJson(qualificationManifest)).digest('hex');
const identifier = z.string().regex(/^[A-Za-z0-9_.:-]{1,128}$/);
const sha256 = z.string().regex(/^[a-f0-9]{64}$/);
const positive = z.number().int().positive();
export const qualificationBinding = z.object({
  nonce: sha256, manifest_digest: z.literal(qualificationDigest), manifest_version: z.literal('e45-v1'),
  evaluation_id: identifier, target: identifier, actor: z.enum(['a1', 'a2', 'b1']),
  tenant_id: identifier, user_id: identifier, team_id: z.union([identifier, z.literal('')]),
  session_id: identifier, run_id: identifier, sandbox_uid: identifier, lease_generation: positive,
  image_digest: z.string().regex(/^sha256:[a-f0-9]{64}$/), attempt: positive, credential_epoch: positive,
  issued_at: positive, expires_at: positive,
  limits: z.object({ duration_seconds: z.literal(120), probe_timeout_seconds: z.literal(5), result_bytes: z.literal(65536), model_turns: z.literal(0) }).strict(),
}).strict();
export const qualificationFixtures = z.object({
  owner: qualificationBinding.refine(binding => binding.actor === 'a1'),
  message_id: identifier, summary_id: identifier, artifact_id: z.string().regex(/^art_[a-f0-9]{12,64}$/),
  memory_id: z.string().regex(/^mem_[a-f0-9]{32}$/), cursor: z.string().min(1).max(2048),
  history_version: positive, memory_version: positive, cursor_expires_at: positive,
  bucket: z.string().regex(/^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$/), object_key: z.string().min(1).max(1024),
  context_table: z.string().regex(/^[A-Za-z0-9_.-]{3,255}$/), secret_canary: z.string().regex(/^[A-Za-z0-9/_+=.@:-]{1,256}$/),
  region: z.string().regex(/^[a-z0-9-]{3,32}$/),
}).strict();
export type QualificationFixtures = z.infer<typeof qualificationFixtures>;
export const qualificationPlan = z.object({
  binding: qualificationBinding,
  manifest: z.unknown().refine(value => canonicalJson(value) === canonicalJson(qualificationManifest)),
  fixtures: qualificationFixtures.optional(),
}).strict();
export const qualificationObservation = z.object({
  probe: identifier, executed: z.boolean(), outcome: z.enum(['allowed', 'denied', 'absent', 'present', 'distinct', 'error', 'timeout']),
  exposure_digests: z.array(sha256).max(4).default([]),
}).strict();
export type QualificationBinding = z.infer<typeof qualificationBinding>;
export type QualificationObservation = z.infer<typeof qualificationObservation>;
export const qualificationClaim = qualificationBinding.pick({ nonce: true, evaluation_id: true, manifest_digest: true });
export const qualificationResult = qualificationClaim.extend({
  source: z.literal('sandbox_runtime'), status: z.enum(['completed', 'failed']),
  observations: z.array(qualificationObservation).max(34), failure: z.enum(['probe_unavailable', 'deadline', 'execution_error']).nullable(),
}).strict();
