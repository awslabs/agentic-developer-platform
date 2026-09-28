import type { ThreadOptions } from "@openai/codex-sdk";
import { restrictedSdkConfig } from "./sdk-config.js";
import { personaSchema, type Capability, type Persona, snapshotPersona, sha256, verifySnapshot } from "./persona.js";

export const HARNESS_CONTRACT_REVISION = "codex-sdk-0.155.1/adp-v1";

export type InvocationSource =
  | { kind: "task-api"; taskId: string; generation: number }
  | { kind: "github" | "gitlab"; eventId: string }
  | { kind: "delegation"; parentRunId: string; operationKey: string };
export interface RepositoryBinding {
  provider: "github" | "gitlab";
  repositoryId: string;
  sourceRevision: string;
}

/** Input from the verified gateway grant, never deserialized request authority.
 * Cryptographic verification, revocation and fencing remain host responsibilities.
 */
export interface VerifiedRunPolicy {
  personaKey: string;
  personaDigest: string;
  compatibilityClass: "codex-sdk";
  harnessRevision: string;
  canonicalModel: string;
  allowedEfforts: readonly Persona["effort"][];
  capabilityLayers: {
    tenant: readonly Capability[]; principal: readonly Capability[];
    run: readonly Capability[]; surface: readonly Capability[]; runtime: readonly Capability[];
  };
  limits: Persona["limits"];
  deadlineMs: number;
}

/** Deterministic preflight. This deliberately cannot mint a runnable SDK session:
 * the host still has to enforce isolation, budget, expiry and operation authority.
 */
export function planVerifiedRun(
  snapshot: ReturnType<typeof snapshotPersona>, policy: VerifiedRunPolicy,
  source: InvocationSource, repository: RepositoryBinding | undefined,
  providerCapabilities: readonly Capability[], nowMs: number,
): { persona: Persona; capabilities: Capability[]; limits: Persona["limits"]; options: ThreadOptions;
  sdkConfig: ReturnType<typeof restrictedSdkConfig>; unavailableOptionalCapabilities: Capability[] } {
  if (sha256(snapshot.definition) !== snapshot.digest) throw new Error("Snapshot digest mismatch");
  verifySnapshot(snapshot);
  const persona = personaSchema.parse(JSON.parse(snapshot.definition));
  if (policy.compatibilityClass !== "codex-sdk" || policy.personaKey !== persona.key
      || policy.personaDigest !== snapshot.digest || !policy.canonicalModel.trim()
      || policy.harnessRevision !== HARNESS_CONTRACT_REVISION) throw new Error("Persona/model/harness binding mismatch");
  if (!persona.surfaces.includes(source.kind)) throw new Error("Invocation surface unsupported");
  if (source.kind === "task-api" && (!source.taskId || !Number.isSafeInteger(source.generation) || source.generation < 1)) {
    throw new Error("Invalid Task API generation binding");
  }
  if (source.kind === "delegation" && (!source.parentRunId || !source.operationKey)) {
    throw new Error("Missing delegation binding");
  }
  if (!policy.allowedEfforts.includes(persona.effort)) throw new Error("Effort not admitted");
  if (!Number.isFinite(nowMs) || !Number.isFinite(policy.deadlineMs) || policy.deadlineMs <= nowMs) {
    throw new Error("Run deadline expired");
  }
  for (const value of Object.values(policy.limits)) {
    if (!Number.isSafeInteger(value) || value <= 0) throw new Error("Invalid policy limit");
  }
  if (repository && (!repository.repositoryId || !repository.sourceRevision)) throw new Error("Invalid repository binding");
  const repositoryCapabilities = new Set<Capability>([
    "repository.read", "repository.write", "branch.push", "change.create",
    "change.update", "review.submit", "change.merge", "story.create",
  ]);
  const permitted = (capability: Capability) =>
    Object.values(policy.capabilityLayers).every(layer => layer.includes(capability))
    && (!repositoryCapabilities.has(capability) || (repository && providerCapabilities.includes(capability)));
  const missing = persona.requiredCapabilities.filter(c => !permitted(c));
  if (missing.length) throw new Error(`Required capabilities unavailable: ${missing.join(", ")}`);
  const capabilities = [...persona.requiredCapabilities, ...persona.optionalCapabilities.filter(permitted)];
  for (const skill of persona.skills) {
    if (skill.requiredCapabilities?.some(c => !capabilities.includes(c))) {
      throw new Error(`Required skill capabilities unavailable: ${skill.id}`);
    }
  }
  const limits = {
    maxTurns: Math.min(persona.limits.maxTurns, policy.limits.maxTurns),
    maxContextBytes: Math.min(persona.limits.maxContextBytes, policy.limits.maxContextBytes),
    maxDurationMs: Math.min(persona.limits.maxDurationMs, policy.limits.maxDurationMs, policy.deadlineMs - nowMs),
  };
  if (Buffer.byteLength(snapshot.instructions) >= limits.maxContextBytes) throw new Error("Instructions exhaust admitted context budget");
  const options: ThreadOptions = {
    model: policy.canonicalModel, modelReasoningEffort: persona.effort,
    skipGitRepoCheck: !repository, networkAccessEnabled: false, webSearchMode: "disabled",
    approvalPolicy: "never", sandboxMode: capabilities.includes("repository.write") ? "workspace-write" : "read-only",
    threadSource: `adp-${persona.key}`,
  };
  return { persona, capabilities, limits, options, sdkConfig: restrictedSdkConfig(),
    unavailableOptionalCapabilities: persona.optionalCapabilities.filter(c => !permitted(c)) };
}
