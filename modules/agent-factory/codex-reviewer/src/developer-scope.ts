import { createHash } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { readTaskBoardFile, taskBoardPath, type Task } from "./task-board.js";

/** Context from the protected engine dispatch, never from an issue or task board.
 * This selects work; it grants no permissions or completion authority. */
export interface DeveloperNodeScope {
  schema_version: 1;
  flow_id: string;
  node_id: string;
  graph_address: string;
  plan_version: number;
  repository: string;
  issue_number: number;
  title: string;
  criteria: string[];
  evidence_expectations: string[];
  delivery_mode: string | null;
}

export function parseDeveloperNodeScope(raw: string | undefined, expected: {
  repository: string; issue: number; flowId?: string; nodeAddress?: string;
}): DeveloperNodeScope | undefined {
  if (!raw?.trim()) return undefined; // Existing non-scoped dispatches remain compatible.
  const value = JSON.parse(raw) as DeveloperNodeScope;
  const strings = [value?.flow_id, value?.node_id, value?.graph_address, value?.repository, value?.title];
  if (value?.schema_version !== 1 || !strings.every(v => typeof v === "string" && v.trim())
      || !Number.isSafeInteger(value.plan_version) || value.plan_version < 1
      || value.repository.toLowerCase() !== expected.repository.toLowerCase()
      || value.issue_number !== expected.issue || value.flow_id !== expected.flowId
      || value.graph_address !== expected.nodeAddress
      || !Array.isArray(value.criteria)
      || !value.criteria.every(v => typeof v === "string" && v.trim())
      || !Array.isArray(value.evidence_expectations) || !value.evidence_expectations.every(v => typeof v === "string")
      || !(value.delivery_mode === null || typeof value.delivery_mode === "string")) {
    throw new Error("Accepted developer node context does not match this assignment");
  }
  return value;
}

export function developerScopeHash(scope: DeveloperNodeScope): string {
  // A plan-only amendment need not discard a board whose exact work is unchanged.
  return createHash("sha256").update(JSON.stringify([
    scope.flow_id, scope.node_id, scope.graph_address, scope.repository.toLowerCase(), scope.issue_number,
    scope.title, scope.criteria, scope.evidence_expectations, scope.delivery_mode,
  ])).digest("hex");
}

export function developerScopePrompt(scope: DeveloperNodeScope): string {
  return `Accepted delivery node: ${scope.graph_address} (plan version ${scope.plan_version}).
Title: ${scope.title}
This assignment covers only this accepted node. The linked issue and its comments provide context for the wider work; they do not expand this node into the whole issue or epic.
Implement and verify every criterion below. Keep later-node work and live obligations deferred, preserve original acceptance/scenario IDs, and use Refs #${scope.issue_number} without closing the shared issue.
${scope.criteria.map(criterion => `- ${criterion}`).join("\n")}
Delivery mode: ${scope.delivery_mode ?? "engine-defined"}. Required evidence: ${scope.evidence_expectations.join(", ") || "engine-defined"}.
Use these exact criterion labels in the task board. Complete only after this node's tasks and checks pass; other nodes retain their own completion requirements. Normal engine review, policy, merge and evidence checks remain in force.`;
}

export async function restoreDeveloperBoard(workspace: string, issue: number, scope?: DeveloperNodeScope): Promise<{
  tasks: Task[] | null; error?: string; archivedPath?: string;
}> {
  const existing = await readTaskBoardFile(workspace, issue);
  if (!scope || !existing.tasks) return existing;
  const raw = await readFile(join(workspace, taskBoardPath(issue)), "utf8");
  const header = JSON.parse(raw);
  if (header.flow_id === scope.flow_id && header.node_address === scope.graph_address
      && header.scope_hash === developerScopeHash(scope)) return existing;
  // A whole-issue, different-node or changed-scope board is historical context.
  // Preserve its exact bytes and commit history; never count its tasks as passed.
  const digest = createHash("sha256").update(raw).digest("hex");
  const archivedPath = `.adp/tasks/archive/${issue}-${digest}.json`;
  await mkdir(join(workspace, ".adp", "tasks", "archive"), { recursive: true });
  try { await writeFile(join(workspace, archivedPath), raw, { flag: "wx" }); }
  catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "EEXIST"
        || await readFile(join(workspace, archivedPath), "utf8") !== raw) throw error;
  }
  return { tasks: null, archivedPath };
}
