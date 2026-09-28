import { z } from "zod";
import { sha256 } from "./persona.js";
import type { RepositoryBinding } from "./admission.js";

const revision = z.string().regex(/^[a-f0-9]{40}(?:[a-f0-9]{24})?$/);
const digest = z.string().regex(/^[a-f0-9]{64}$/);
const reference = z.string().min(1).max(512);
const key = z.string().min(1).max(200);
const validation = z.strictObject({ check: key, commit: revision, specificationDigest: digest, environmentDigest: digest,
  status: z.enum(["passed", "failed", "unknown"]), receipt: reference });
const evidenceSchema = z.strictObject({
  runId: key, provider: z.enum(["github", "gitlab"]), repositoryId: key, sourceRevision: revision,
  head: revision, clean: z.boolean(), publication: z.strictObject({ number: z.number().int().positive().safe(),
    state: z.enum(["open", "closed", "merged"]), draft: z.boolean(), head: revision, receipt: reference }),
  requirements: z.array(z.strictObject({ id: digest, commit: revision, status: z.enum(["met", "unmet", "unknown"]),
    receipts: z.array(reference).min(1).max(32) })).max(100),
  validations: z.array(validation).max(128),
  review: z.strictObject({ commit: revision, verdict: z.enum(["approved", "changes_requested", "unknown"]),
    unresolvedFindings: z.number().int().nonnegative(), receipt: reference }).optional(),
  merge: z.strictObject({ reviewedHead: revision, commit: revision, protectionsSatisfied: z.boolean(), receipt: reference }).optional(),
});

export interface CompletionRequirements {
  runId: string;
  policy: "validated-change" | "review-repair-merge";
  repository: RepositoryBinding;
  /** Immutable admitted criteria, including committed user amendments. */
  criteria: readonly string[];
  checks: readonly { check: string; specificationDigest: string; environmentDigest: string }[];
  assignedChange?: number;
}
export interface CompletionHost {
  assertCurrent(signal: AbortSignal): Promise<void>;
  /** Read protected journal/provider/validation records, never model-authored
   * reports or claims. Each returned reference must resolve to a durable receipt
   * bound to this run, repository and commit. Missing records must fail closed. */
  readEvidence(signal: AbortSignal): Promise<unknown>;
}
export function requirementId(index: number, criterion: string): string {
  if (!Number.isSafeInteger(index) || index < 0 || !criterion.trim()) throw new Error("Invalid completion requirement");
  return sha256(`${index}\0${criterion}`);
}

/** Shared finalization gate for developer and reviewer. Publication and merge
 * happen through authorized host tools; completion independently checks their
 * durable outcomes. A model's final prose is deliberately not an input. */
export async function verifyRepositoryCompletion(requirements: CompletionRequirements, host: CompletionHost, signal: AbortSignal) {
  const required = structuredClone(requirements);
  const { repository, criteria, checks } = required;
  if (!required.runId || !["validated-change", "review-repair-merge"].includes(required.policy)
    || !criteria.length || criteria.length > 100 || !checks.length || checks.length > 128
    || new Set(checks.map(item => item.check)).size !== checks.length) throw new Error("Incomplete host completion policy");
  const expectedCriteria = criteria.map((criterion, index) => requirementId(index, criterion));
  for (const check of checks) { key.parse(check.check); digest.parse(check.specificationDigest); digest.parse(check.environmentDigest); }
  signal.throwIfAborted();
  await host.assertCurrent(signal);
  const evidence = evidenceSchema.parse(await host.readEvidence(signal));
  signal.throwIfAborted();
  if (evidence.runId !== required.runId || evidence.provider !== repository.provider || evidence.repositoryId !== repository.repositoryId
    || evidence.sourceRevision !== repository.sourceRevision || !evidence.clean || evidence.publication.head !== evidence.head
    || evidence.publication.draft || (required.assignedChange !== undefined && evidence.publication.number !== required.assignedChange)) {
    throw new Error("Completion repository/publication binding mismatch");
  }
  if (evidence.requirements.length !== expectedCriteria.length || new Set(evidence.requirements.map(item => item.id)).size !== expectedCriteria.length
    || expectedCriteria.some(id => !evidence.requirements.some(item => item.id === id && item.commit === evidence.head && item.status === "met"))) {
    throw new Error("Completion lacks final-commit requirement evidence");
  }
  // Conflicting or stale receipts cannot be hidden behind another passing row.
  if (new Set(evidence.validations.map(item => item.check)).size !== evidence.validations.length
    || checks.some(check => !evidence.validations.some(item => item.check === check.check && item.commit === evidence.head
      && item.status === "passed" && item.specificationDigest === check.specificationDigest && item.environmentDigest === check.environmentDigest))) {
    throw new Error("Completion lacks required final-commit validation");
  }
  if (required.policy === "validated-change") {
    if (evidence.publication.state !== "open" || evidence.merge) throw new Error("Developer must leave a ready open change");
  } else if (evidence.publication.state !== "merged" || evidence.review?.commit !== evidence.head || evidence.review.verdict !== "approved"
    || evidence.review.unresolvedFindings !== 0 || evidence.merge?.reviewedHead !== evidence.head || !evidence.merge.protectionsSatisfied) {
    throw new Error("Reviewer completion requires reviewed, validated and confirmed merge");
  }
  await host.assertCurrent(signal);
  signal.throwIfAborted();
  return Object.freeze({ runId: evidence.runId, change: evidence.publication.number, head: evidence.head,
    ...(evidence.merge ? { mergeCommit: evidence.merge.commit } : {}),
    receipts: Object.freeze([evidence.publication.receipt, ...evidence.requirements.flatMap(item => item.receipts),
      ...evidence.validations.map(item => item.receipt), ...(evidence.review ? [evidence.review.receipt] : []),
      ...(evidence.merge ? [evidence.merge.receipt] : [])]) });
}
