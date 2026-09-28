// Configuration
export interface Config {
  awsRegion: string;
  secretPrefix: string;
  pollingInterval: number;
  maxRetries: number;
  logLevel: 'DEBUG' | 'INFO' | 'WARN' | 'ERROR';
  bedrockModel: string;
}

// GitHub Context
export interface IssueContext {
  owner: string;
  repo: string;
  issueNumber: number;
  issueTitle: string;
  issueBody: string;
  labels: string[];
  retryGuidance?: string;  // Additional guidance from /retry comment
}

// Agent State
export interface AgentState {
  issueContext: IssueContext;
  phase: 'planning' | 'awaiting_approval' | 'code_generation' | 'complete' | 'error';
  plan: Plan | null;
  checklistCommentId: number | null;
  planCommentId: number | null;
  workDir: string;
  startTime: string;
  errorCount: number;
}

// Plan
export interface Plan {
  summary: string;
  steps: PlanStep[];
  estimatedFiles: string[];
}

export interface PlanStep {
  description: string;
  completed: boolean;
}

// Progress Tracking
export interface ChecklistItem {
  label: string;
  completed: boolean;
}

export interface Milestone {
  name: string;
  description: string;
  timestamp: string;
}

// GitHub App Credentials
export interface GitHubAppCredentials {
  appId: string;
  privateKey: string;
  installationId: string;
}

// Approval Result
//
// Fail-closed outcome vocabulary (issue #4181). There is deliberately NO
// permissive value other than 'allowed-once' and no durable/always-allow grant:
// every caller must treat anything that is not 'allowed-once' as non-permissive.
//
//   'allowed-once' — an authorized approver approved THIS request. The only
//                    value that permits the action to proceed.
//   'rejected'     — an authorized approver denied it, OR the wait expired with
//                    no authorized answer. Deny-on-expiry is the default.
//   'cancelled'    — the request was withdrawn before it was answered.
//   'unavailable'  — we could not ask (e.g. persistent GitHub API failure).
//                    Non-permissive like 'rejected', but kept distinct so the
//                    audit trail never records a transport failure as a human
//                    denial.
export type ApprovalOutcome = 'allowed-once' | 'rejected' | 'cancelled' | 'unavailable';

export interface ApprovalResult {
  outcome: ApprovalOutcome;
  /** Login of the authorized approver, when a human answered. Recorded for audit. */
  approver?: string;
  feedback?: string;
  comment?: string;
}

// Code Generation Result
export interface CodeResult {
  success: boolean;
  filesModified: string[];
  filesCreated: string[];
  error?: string;
}

// Lock Info
export interface LockInfo {
  issueId: string;
  startTime: string;
  pid: number;
}

// Error Types
export interface RetryableError extends Error {
  retryable: boolean;
  statusCode?: number;
}

// Logger Context
export interface LogContext {
  issueNumber?: number;
  phase?: string;
  component?: string;
  [key: string]: unknown;
}
