import { withIssueAuthoring } from './issue-authoring';

/** PM entry points use the shared installed epic/story authoring contract. */
export function withPmIssueAuthoring(prompt: string, roots?: readonly string[]): string {
  return withIssueAuthoring(prompt, 'PM', roots);
}
