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
export interface ApprovalResult {
  approved: boolean;
  rejected: boolean;
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
