import { run } from "./process.js";

export interface ReviewerChecks {
  head_sha: string;
  base_sha: string;
  state: "passed" | "pending" | "failed";
  open: boolean;
  merged: boolean;
  base_repair_required: boolean;
  reasons: string[];
  checks: unknown[];
  failures: unknown[];
  merge_state?: "eligible" | "waiting" | "blocked";
  merge_reasons?: string[];
  merge_method?: "squash" | "merge" | "rebase" | "queue";
  pr_node_id?: string;
}

export async function observeReviewerChecks(head: string, forMerge = false): Promise<ReviewerChecks> {
  const response = await run("python3", ["-m", "lib.reviewer_checks", head, ...(forMerge ? ["--for-merge"] : [])]);
  const result = JSON.parse(response.stdout);
  if (result.error) throw Object.assign(new Error(`Reviewer checks unavailable: ${result.error}`), { retryable: result.retryable });
  if (result.contract_version !== 1 || result.head_sha !== head || !/^[a-f0-9]{40}$/.test(result.base_sha)
      || !["passed", "pending", "failed"].includes(result.state)
      || ["open", "merged", "base_repair_required"].some(key => typeof result[key] !== "boolean")
      || ["reasons", "checks", "failures"].some(key => !Array.isArray(result[key]))) {
    throw new Error("Invalid reviewer checks observation");
  }
  if (forMerge && (!['eligible', 'waiting', 'blocked'].includes(result.merge_state)
      || !Array.isArray(result.merge_reasons) || typeof result.pr_node_id !== "string"
      || (result.merge_state === "eligible" && !['squash', 'merge', 'rebase', 'queue'].includes(result.merge_method)))) {
    throw new Error("Invalid merge authorization observation");
  }
  return result;
}
