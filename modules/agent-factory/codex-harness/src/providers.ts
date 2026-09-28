import { z } from "zod";
import type { Capability } from "./persona.js";

export type Provider = "github" | "gitlab";
export interface ProviderOperation {
  capability: Capability;
  provider: Provider;
  repository: string;
  method: "GET" | "POST" | "PUT" | "PATCH";
  /** Relative to the host-selected provider REST root (GitLab: /api/v4). */
  path: string;
  body?: Record<string, unknown>;
  operationKey?: string;
}
/** Host-owned transport: authenticate/revalidate the run, enforce repository scope
 * and capability, and journal mutations by operationKey BEFORE sending them.
 * An unknown provider outcome must be reconciled, never blindly replayed.
 * No provider token, endpoint or arbitrary HTTP method is supplied by a persona.
 */
export interface ProviderBroker {
  execute(operation: ProviderOperation): Promise<unknown>;
}
export interface ChangeRequest {
  number: number;
  state: "open" | "closed" | "merged";
  head: string;
  branch: string;
  base: string;
  title: string;
  description: string;
}
const headSchema = z.string().regex(/^[a-f0-9]{40}(?:[a-f0-9]{24})?$/);
const branchSchema = z.string().min(1).max(255).refine(value =>
  !/[\x00-\x20\x7f~^:?*\[\\]/.test(value) && !value.includes("..")
  && !value.includes("@{") && !value.startsWith("/") && !value.endsWith("/")
  && !value.endsWith(".") && !value.endsWith(".lock") && !value.includes("//"));
const numberSchema = z.number().int().positive().safe();
const keySchema = z.string().regex(/^[a-zA-Z0-9][a-zA-Z0-9:._-]{0,199}$/);
const titleSchema = z.string().min(1).max(255);
const descriptionSchema = z.string().max(32768);
const githubChangeSchema = z.object({
  number: numberSchema, state: z.enum(["open", "closed"]), merged: z.boolean().optional(),
  head: z.object({ sha: headSchema, ref: branchSchema }), base: z.object({ ref: branchSchema }),
  title: titleSchema, body: descriptionSchema.nullable(),
});
const gitlabChangeSchema = z.object({
  iid: numberSchema, state: z.enum(["opened", "closed", "merged"]), sha: headSchema,
  source_branch: branchSchema, target_branch: branchSchema,
  title: titleSchema, description: descriptionSchema.nullable(),
});

/** Provider semantics only. This class does not grant authority, fetch tokens,
 * make direct network calls or decide whether review/validation is complete. */
export class RepositoryAdapter {
  readonly capabilities: readonly Capability[];
  private readonly root: string;
  constructor(readonly provider: Provider, readonly repository: string, private readonly broker: ProviderBroker) {
    if (!repository || repository.length > 512 || !/^[A-Za-z0-9_.-]+(?:\/[A-Za-z0-9_.-]+)*$/.test(repository)
        || repository.split("/").some(part => part === "." || part === "..")) throw new Error("Invalid repository identifier");
    if (provider === "github") {
      if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repository)) throw new Error("Invalid GitHub repository");
      this.root = `/repos/${repository}/pulls`;
      this.capabilities = Object.freeze(["repository.read", "change.create", "change.update", "review.submit", "change.merge"]);
    } else if (provider === "gitlab") {
      this.root = `/projects/${encodeURIComponent(repository)}/merge_requests`;
      // Formal approve/request-changes parity is not a GitLab MR note. Until
      // its approval contract is implemented, fail required review admission.
      this.capabilities = Object.freeze(["repository.read", "change.create", "change.update", "change.merge"]);
    } else throw new Error("Unknown repository provider");
  }
  private async call(capability: Capability, method: ProviderOperation["method"], suffix: string,
                     body?: Record<string, unknown>, operationKey?: string) {
    if (!this.capabilities.includes(capability)) throw new Error("Provider capability not implemented");
    if (method !== "GET") keySchema.parse(operationKey);
    return this.broker.execute({ provider: this.provider, repository: this.repository,
      capability, method, path: this.root + suffix, ...(body ? { body } : {}),
      ...(operationKey ? { operationKey } : {}) });
  }
  private normalize(raw: unknown): ChangeRequest {
    if (this.provider === "github") {
      const pr = githubChangeSchema.parse(raw);
      return { number: pr.number, state: pr.merged ? "merged" : pr.state, head: pr.head.sha,
        branch: pr.head.ref, base: pr.base.ref, title: pr.title, description: pr.body ?? "" };
    }
    const mr = gitlabChangeSchema.parse(raw);
    return { number: mr.iid, state: mr.state === "opened" ? "open" : mr.state, head: mr.sha,
      branch: mr.source_branch, base: mr.target_branch, title: mr.title, description: mr.description ?? "" };
  }
  async readChange(number: number): Promise<ChangeRequest> {
    numberSchema.parse(number);
    const change = this.normalize(await this.call("repository.read", "GET", `/${number}`));
    if (change.number !== number) throw new Error("Provider returned a different change request");
    return change;
  }
  async createChange(input: { branch: string; base: string; title: string; description: string; operationKey: string }) {
    const branch = branchSchema.parse(input.branch), base = branchSchema.parse(input.base);
    if (branch === base) throw new Error("Source and target branch must differ");
    const title = titleSchema.parse(input.title), description = descriptionSchema.parse(input.description);
    const body = this.provider === "github"
      ? { head: branch, base, title, body: description, draft: false }
      : { source_branch: branch, target_branch: base, title, description };
    const change = this.normalize(await this.call("change.create", "POST", "", body, input.operationKey));
    if (change.branch !== branch || change.base !== base) throw new Error("Provider created an unexpected branch binding");
    return change;
  }
  async updateDescription(number: number, description: string, operationKey: string) {
    numberSchema.parse(number); descriptionSchema.parse(description);
    const change = this.normalize(await this.call("change.update", this.provider === "github" ? "PATCH" : "PUT",
      `/${number}`, this.provider === "github" ? { body: description } : { description }, operationKey));
    if (change.number !== number) throw new Error("Provider returned a different change request");
    return change;
  }
  async submitReview(number: number, expectedHead: string, verdict: "approve" | "request_changes", body: string, operationKey: string) {
    numberSchema.parse(number); headSchema.parse(expectedHead); descriptionSchema.parse(body);
    if (!["approve", "request_changes"].includes(verdict)) throw new Error("Invalid review verdict");
    return this.call("review.submit", "POST", `/${number}/reviews`, {
      commit_id: expectedHead, event: verdict === "approve" ? "APPROVE" : "REQUEST_CHANGES", body,
    }, operationKey);
  }
  async merge(number: number, expectedHead: string, operationKey: string): Promise<{ commit: string }> {
    numberSchema.parse(number); headSchema.parse(expectedHead);
    // No force/admin/bypass option. The broker must verify the final-commit
    // evidence and current approval policy; the provider enforces its protections.
    const raw = await this.call("change.merge", "PUT", `/${number}/merge`, this.provider === "github"
      ? { sha: expectedHead, merge_method: "squash" }
      : { sha: expectedHead, squash: true, auto_merge: false }, operationKey);
    if (this.provider === "github") {
      const result = z.object({ merged: z.literal(true), sha: headSchema }).parse(raw);
      return { commit: result.sha };
    }
    const result = z.object({ state: z.literal("merged"), merge_commit_sha: headSchema }).parse(raw);
    return { commit: result.merge_commit_sha };
  }
}
