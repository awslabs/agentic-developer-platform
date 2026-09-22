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
}

export async function observeReviewerChecks(head: string): Promise<ReviewerChecks> {
  const response = await run("python3", ["-m", "lib.reviewer_checks", head]);
  const result = JSON.parse(response.stdout);
  if (result.error) throw Object.assign(new Error(`Reviewer checks unavailable: ${result.error}`), { retryable: result.retryable });
  if (result.contract_version !== 1 || result.head_sha !== head || !/^[a-f0-9]{40}$/.test(result.base_sha)
      || !["passed", "pending", "failed"].includes(result.state)
      || ["open", "merged", "base_repair_required"].some(key => typeof result[key] !== "boolean")
      || ["reasons", "checks", "failures"].some(key => !Array.isArray(result[key]))) {
    throw new Error("Invalid reviewer checks observation");
  }
  return result;
}
