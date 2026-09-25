import { z } from "zod";
import { capabilitySchema, personaSchema, verifySnapshot } from "./persona.js";
import { HARNESS_CONTRACT_REVISION, type VerifiedRunPolicy } from "./admission.js";

const digest = z.string().regex(/^[a-f0-9]{64}$/);
const capabilities = z.array(capabilitySchema).max(32);
const harnessSchema = z.strictObject({
  snapshot: z.strictObject({ definition: z.string().max(65536), digest, instructions: z.string().max(262144), skillSources: z.string().max(2097152) }),
  policy: z.strictObject({
    personaKey: z.string(), personaDigest: digest, compatibilityClass: z.literal("codex-sdk"),
    harnessRevision: z.literal(HARNESS_CONTRACT_REVISION), canonicalModel: z.string().min(1).max(128),
    allowedEfforts: z.array(z.enum(["low", "medium", "high", "xhigh"])).min(1).max(4),
    capabilityLayers: z.strictObject({ tenant: capabilities, principal: capabilities, run: capabilities, surface: capabilities, runtime: capabilities }),
    limits: personaSchema.shape.limits, deadlineMs: z.number().int().positive().safe(),
  }),
});

/** Validate trusted-host bootstrap metadata, never derive authority from task text.
 * Gateway admission must freeze these fields before this entrypoint is registered.
 */
export function taskHarness(start: {
  harness: unknown; persona: string; deadline_at: string;
  model_binding: { model_id: string; transport: string; invocability_verified: boolean };
  limits: { max_turns: number; max_output_tokens_per_turn: number };
}) {
  const parsed = harnessSchema.parse(start.harness);
  const snapshot = verifySnapshot(parsed.snapshot);
  const policy: VerifiedRunPolicy = parsed.policy;
  if (start.persona !== `agent-task-${policy.personaKey}` || start.model_binding.transport !== "openai_responses"
    || start.model_binding.invocability_verified !== true || start.model_binding.model_id !== policy.canonicalModel
    || policy.personaDigest !== snapshot.digest || !Number.isFinite(Date.parse(start.deadline_at))
    || policy.deadlineMs > Date.parse(start.deadline_at)
    || policy.limits.maxTurns > start.limits.max_turns) throw new Error("Task harness bootstrap binding mismatch");
  return { snapshot, policy };
}

interface Citation { ref: string; source: string; artifact_id?: string }
interface Report { findings: { evidence_refs: string[] }[]; evidence_refs: Citation[] }

/** Reuse the Task report schema, then require host-known citation identities.
 * Structural grounding does not certify the semantic truth of model findings.
 */
export function parseTaskReport(raw: string, evidence: ReadonlyMap<string, Citation>, assertReport: (value: unknown) => void) {
  const report: unknown = JSON.parse(raw);
  assertReport(report);
  const parsed = report as Report;
  const declared = new Set<string>();
  for (const citation of parsed.evidence_refs) {
    const actual = evidence.get(citation.ref);
    if (declared.has(citation.ref) || !actual || actual.source !== citation.source || actual.artifact_id !== citation.artifact_id) {
      throw new Error("Task report cites unsupported evidence");
    }
    declared.add(citation.ref);
  }
  if (parsed.findings.some(finding => finding.evidence_refs.some(ref => !declared.has(ref)))) throw new Error("Task finding has an undeclared citation");
  return report;
}
