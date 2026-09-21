export type FixClass = "none" | "mechanical" | "author_required";

export interface ReviewFinding {
  id: string;
  title: string;
  impact: "critical" | "high" | "medium" | "low";
  confidence: "high" | "medium" | "low";
  blocking: boolean;
  fixClass: FixClass;
  details: string;
  file: string;
  line: number | null;
  recommendedFix: string;
}

export interface ReviewVerdict {
  verdict: "approve" | "request_changes";
  summary: string;
  findings: ReviewFinding[];
  validationGaps: string[];
}

export function requiresChanges(verdict: ReviewVerdict): boolean {
  return (
    verdict.verdict === "request_changes" ||
    verdict.findings.some((finding) => finding.blocking)
  );
}

interface CodexEnvelopeBase {
  version: "1.0";
  message_id: string;
  arrived_at: string;
  tenant_id: string;
  installation_id: number;
  repository: string;
  correlation?: {
    correlation_id?: string;
    root_human_id?: string;
    parent_invocation_id?: string | null;
  };
}

export interface CodexPullRequestReviewEnvelope extends CodexEnvelopeBase {
  kind: "codex_pr_review";
  pull_request: {
    number: number;
    issue_number: number;
    head_ref: string;
    base_ref: string;
    expected_head_sha: string;
    html_url: string;
  };
}

export interface CodexIssueReviewEnvelope extends CodexEnvelopeBase {
  kind: "codex_issue_review";
  issue: {
    number: number;
    triggering_comment: string;
  };
}

export interface CodexEngineReviewEnvelope extends CodexEnvelopeBase {
  kind: "codex_engine_review";
  issue_number: number;
  cycle: {
    action: "review" | "repair";
    repo: string;
    pr_number: number;
    head_sha: string;
    findings: unknown[];
    allow_story_repairs: boolean;
  };
}

export type CodexReviewEnvelope =
  | CodexEngineReviewEnvelope
  | CodexPullRequestReviewEnvelope
  | CodexIssueReviewEnvelope;

const SHA_RE = /^[0-9a-f]{40}$/;
const REPO_RE = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/;
const AGENT_BRANCH_RE = /^agent\/issue-(\d+)(?:-[A-Za-z0-9._-]+)?$/;

export function issueFromAgentBranch(branch: string): number | null {
  const match = AGENT_BRANCH_RE.exec(branch);
  if (!match?.[1]) return null;
  const issue = Number(match[1]);
  return Number.isSafeInteger(issue) && issue > 0 ? issue : null;
}

function requiredString(value: unknown, name: string): string {
  if (typeof value !== "string" || value.trim() === "") {
    throw new Error(`${name} must be a non-empty string`);
  }
  return value;
}

function requiredPositiveInteger(value: unknown, name: string): number {
  if (!Number.isSafeInteger(value) || (value as number) <= 0) {
    throw new Error(`${name} must be a positive integer`);
  }
  return value as number;
}

export function parseEnvelope(raw: string): CodexReviewEnvelope {
  const value = JSON.parse(raw) as Record<string, unknown>;
  if (value.version !== "1.0" || value.persona !== "agent-codex-reviewer") {
    throw new Error("unsupported codex reviewer envelope");
  }
  const payload = value.payload as Record<string, unknown> | undefined;
  const sourceRef = value.source_ref as Record<string, unknown> | undefined;
  const repository = requiredString(sourceRef?.repo, "repository");
  if (!REPO_RE.test(repository)) throw new Error("repository is invalid");
  const common = {
    version: "1.0" as const,
    message_id: requiredString(value.message_id, "message_id"),
    arrived_at: requiredString(value.arrived_at, "arrived_at"),
    tenant_id: requiredString(value.tenant_id, "tenant_id"),
    installation_id: requiredPositiveInteger(
      sourceRef?.installation_id,
      "installation_id",
    ),
    repository,
    correlation:
      typeof value.correlation === "object" && value.correlation !== null
        ? (value.correlation as CodexReviewEnvelope["correlation"])
        : undefined,
  };
  if (value.review_cycle_input !== undefined) {
    const cycle = value.review_cycle_input as Record<string, unknown>;
    const intent = value.intent as Record<string, unknown> | undefined;
    if (!cycle || intent?.trigger !== "engine_review_cycle"
        || !["review", "repair"].includes(String(cycle.action))
        || cycle.repo !== repository || !SHA_RE.test(String(cycle.head_sha))
        || !Array.isArray(cycle.findings) || !cycle.operation_key || !cycle.accepted_scope
        || Buffer.byteLength(JSON.stringify(cycle), "utf8") > 32768) {
      throw new Error("invalid engine review-cycle input");
    }
    return {
      ...common,
      kind: "codex_engine_review",
      issue_number: requiredPositiveInteger(sourceRef?.issue, "issue.number"),
      cycle: {
        action: cycle.action as "review" | "repair",
        repo: repository,
        pr_number: requiredPositiveInteger(cycle.pr_number, "pull_request.number"),
        head_sha: cycle.head_sha as string,
        findings: cycle.findings,
        allow_story_repairs: cycle.allow_story_repairs === true,
      },
    };
  }
  if (!payload) throw new Error("payload is required");
  const pr = payload?.pull_request as Record<string, unknown> | undefined;
  if (!pr) {
    const issue = payload.issue as Record<string, unknown> | undefined;
    const comment = payload.comment as Record<string, unknown> | undefined;
    const issueNumber = requiredPositiveInteger(sourceRef?.issue, "issue.number");
    const payloadIssueNumber = requiredPositiveInteger(
      issue?.number,
      "payload.issue.number",
    );
    if (issueNumber !== payloadIssueNumber) {
      throw new Error("source_ref.issue does not match payload.issue.number");
    }
    return {
      ...common,
      kind: "codex_issue_review",
      issue: {
        number: issueNumber,
        triggering_comment: requiredString(
          comment?.body,
          "issue.triggering_comment",
        ),
      },
    };
  }

  const head = pr.head as Record<string, unknown> | undefined;
  const base = pr.base as Record<string, unknown> | undefined;
  const headRef = requiredString(
    head?.ref,
    "pull_request.head_ref",
  );
  const branchIssue = issueFromAgentBranch(headRef);
  if (branchIssue === null) throw new Error("pull_request head is not an agent issue branch");
  const expectedSha = requiredString(
    head?.sha,
    "pull_request.expected_head_sha",
  );
  if (!SHA_RE.test(expectedSha)) throw new Error("expected_head_sha is invalid");

  return {
    ...common,
    kind: "codex_pr_review",
    pull_request: {
      number: requiredPositiveInteger(
        sourceRef?.pr,
        "pull_request.number",
      ),
      issue_number: branchIssue,
      head_ref: headRef,
      base_ref: requiredString(
        base?.ref,
        "pull_request.base_ref",
      ),
      expected_head_sha: expectedSha,
      html_url: requiredString(pr.html_url, "pull_request.html_url"),
    },
  };
}

export const reviewOutputSchema = {
  type: "object",
  properties: {
    verdict: { type: "string", enum: ["approve", "request_changes"] },
    summary: { type: "string" },
    findings: {
      type: "array",
      items: {
        type: "object",
        properties: {
          id: { type: "string" },
          title: { type: "string" },
          impact: {
            type: "string",
            enum: ["critical", "high", "medium", "low"],
          },
          confidence: { type: "string", enum: ["high", "medium", "low"] },
          blocking: { type: "boolean" },
          fixClass: {
            type: "string",
            enum: ["none", "mechanical", "author_required"],
          },
          details: { type: "string" },
          file: { type: "string" },
          line: { type: ["integer", "null"] },
          recommendedFix: { type: "string" },
        },
        required: [
          "id",
          "title",
          "impact",
          "confidence",
          "blocking",
          "fixClass",
          "details",
          "file",
          "line",
          "recommendedFix",
        ],
        additionalProperties: false,
      },
    },
    validationGaps: { type: "array", items: { type: "string" } },
  },
  required: ["verdict", "summary", "findings", "validationGaps"],
  additionalProperties: false,
} as const;

export function parseVerdict(raw: string): ReviewVerdict {
  const parsed = JSON.parse(raw) as ReviewVerdict;
  if (!parsed || !["approve", "request_changes"].includes(parsed.verdict)) {
    throw new Error("Codex returned an invalid verdict");
  }
  if (!Array.isArray(parsed.findings) || !Array.isArray(parsed.validationGaps)) {
    throw new Error("Codex returned an invalid findings shape");
  }
  if (parsed.findings.some((finding) => finding.blocking)) {
    parsed.verdict = "request_changes";
  }
  return parsed;
}
