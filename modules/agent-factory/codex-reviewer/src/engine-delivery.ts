/** Deterministic publication and merge. No model or scheduler runs here. */
import { createHash } from "node:crypto";
import type { CodexEngineReviewEnvelope } from "./contracts.js";
import type { EngineReviewResult } from "./engine-review.js";
import { GitHubClient, GitHubRequestError } from "./github.js";
import { run } from "./process.js";
import { observeReviewerChecks, type ReviewerChecks } from "./reviewer-checks.js";

export interface ReviewerMerge {
  state: "merged" | "pending" | "repair" | "blocked";
  reason?: string;
  queued?: boolean;
}

async function publish(result: EngineReviewResult, envelope: CodexEngineReviewEnvelope): Promise<void> {
  const output = await run("python3", ["-m", "lib.codex_review_delivery"], {
    input: JSON.stringify({ result, cycle: envelope.cycle }),
  });
  const receipt = JSON.parse(output.stdout);
  if (receipt.error) throw Object.assign(new Error(receipt.error), { retryable: receipt.retryable });
  if (receipt.recorded !== true || receipt.head_sha !== result.sha) throw new Error("Review evidence was not acknowledged");
}

export async function deliverEngineReview(
  github: Pick<GitHubClient, "getPullRequest" | "getBranch" | "merge" | "queueEntry" | "enqueue">,
  result: EngineReviewResult, envelope: CodexEngineReviewEnvelope,
  publication = publish, checks: (head: string, forMerge: boolean) => Promise<ReviewerChecks> = observeReviewerChecks,
): Promise<ReviewerMerge> {
  const number = envelope.cycle.pr_number;
  const current = await github.getPullRequest(number);
  if (current.head.sha !== result.sha) return { state: "blocked", reason: "PR head changed after review" };
  // Reconcile first, including a merge whose HTTP response was lost.
  if (current.merged) return { state: "merged" };
  if (current.state !== "open") return { state: "blocked", reason: "PR closed without merge" };
  await publication(result, envelope);
  const observed = await checks(result.sha, true);
  if (observed.head_sha !== result.sha) return { state: "blocked", reason: "PR head changed after publication" };
  if (observed.merged) return { state: "merged" };
  if (observed.merge_method === "queue" && observed.pr_node_id && await github.queueEntry(observed.pr_node_id, result.sha)) {
    return { state: "pending", queued: true };
  }
  if (observed.base_sha !== result.reviewed_base_sha || observed.base_repair_required || observed.state === "failed") {
    return { state: "repair", reason: JSON.stringify(observed) };
  }
  if (observed.state === "pending" || observed.merge_state === "waiting") return { state: "pending" };
  if (observed.merge_state !== "eligible" || !observed.merge_method || !observed.pr_node_id) {
    return { state: "blocked", reason: (observed.merge_reasons ?? ["Merge authorization unavailable"]).join(", ") };
  }
  // Check the live target once more before the expected-head GitHub mutation.
  if ((await github.getBranch(current.base.ref)).commit.sha !== observed.base_sha) {
    return { state: "repair", reason: "Repair out-of-date base" };
  }
  try {
    if (observed.merge_method === "queue") {
      if (!await github.queueEntry(observed.pr_node_id, result.sha)) {
        const operation = createHash("sha256").update(`${envelope.message_id}:${result.sha}`).digest("hex");
        await github.enqueue(observed.pr_node_id, result.sha, operation);
      }
    } else {
      await github.merge(number, result.sha, observed.merge_method);
    }
  } catch (error) {
    const after = await github.getPullRequest(number);
    if (after.head.sha === result.sha && after.merged) return { state: "merged" };
    if (error instanceof GitHubRequestError && [401, 403, 404, 422].includes(error.status)) {
      return { state: "blocked", reason: `GitHub refused merge (${error.status})` };
    }
    // A conflict may be a new base or check result. Reobserve before deciding
    // whether a repair is needed. Unknown transport outcomes never imply merge.
    if (error instanceof GitHubRequestError && [405, 409].includes(error.status)) return { state: "pending" };
    throw Object.assign(new Error("Merge outcome unavailable; reconcile before retry"), { retryable: true });
  }
  const after = await github.getPullRequest(number);
  return { state: after.head.sha === result.sha && after.merged ? "merged" : "pending", queued: observed.merge_method === "queue" };
}
