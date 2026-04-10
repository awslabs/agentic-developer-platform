/**
 * PM Poll Analyzer - AI-powered failure analysis for agent workflows
 *
 * When an agent workflow fails, this module:
 * 1. Fetches the workflow logs
 * 2. Gathers issue context
 * 3. Uses Claude to analyze the failure
 * 4. Proposes resolutions to progress the workflow
 */

import { resilientQuery } from './utils/resilientQuery';
import { execSync } from 'child_process';

// ============================================================================
// Types
// ============================================================================

export interface WorkflowRunContext {
  runId: number;
  workflowName: string;
  conclusion: 'success' | 'failure' | 'cancelled' | 'skipped';
  htmlUrl: string;
  headSha: string;
  runAttempt: number;
}

export interface IssueContext {
  number: number;
  title: string;
  body: string;
  labels: string[];
  state: 'open' | 'closed';
}

export interface FailureAnalysis {
  summary: string;
  rootCause: string;
  recommendations: Recommendation[];
  canAutoResolve: boolean;
}

export interface Recommendation {
  id: number;
  action: 'retry' | 'fix_config' | 'manual_intervention' | 'skip' | 'escalate';
  description: string;
  command?: string;
  confidence: 'high' | 'medium' | 'low';
}

// ============================================================================
// Configuration
// ============================================================================

let config = {
  enabled: true,
  maxLogLines: 500,
  repoOwner: '',
  repoName: '',
};

export function configurePollAnalyzer(options: Partial<typeof config>) {
  config = { ...config, ...options };
}

export function isAnalyzerEnabled(): boolean {
  return config.enabled && process.env.PM_POLL_AI_ENABLED !== 'false';
}

// ============================================================================
// Log Fetching
// ============================================================================

function execCommand(cmd: string): string {
  try {
    return execSync(cmd, { encoding: 'utf-8', maxBuffer: 10 * 1024 * 1024 }).trim();
  } catch (error) {
    const err = error as { stderr?: string; message?: string };
    console.error(`Command failed: ${cmd}`);
    console.error(`Error: ${err.stderr || err.message}`);
    return '';
  }
}

export async function fetchWorkflowLogs(runId: number): Promise<string> {
  const { repoOwner, repoName, maxLogLines } = config;

  // Get failed jobs from the workflow run
  const jobsJson = execCommand(
    `gh api repos/${repoOwner}/${repoName}/actions/runs/${runId}/jobs --jq '.jobs[] | select(.conclusion == "failure") | {id: .id, name: .name}'`
  );

  if (!jobsJson) {
    return 'Unable to fetch workflow jobs';
  }

  // Parse jobs (may be multiple lines of JSON objects)
  const jobs: { id: number; name: string }[] = [];
  for (const line of jobsJson.split('\n').filter(Boolean)) {
    try {
      jobs.push(JSON.parse(line));
    } catch {
      // Skip malformed lines
    }
  }

  if (jobs.length === 0) {
    return 'No failed jobs found';
  }

  // Fetch logs for each failed job
  const allLogs: string[] = [];
  for (const job of jobs) {
    const logsCmd = `gh api repos/${repoOwner}/${repoName}/actions/jobs/${job.id}/logs 2>/dev/null | tail -${maxLogLines}`;
    const logs = execCommand(logsCmd);
    if (logs) {
      allLogs.push(`=== Job: ${job.name} ===\n${logs}`);
    }
  }

  return allLogs.join('\n\n') || 'Unable to fetch job logs';
}

export async function fetchIssueFromWorkflow(runId: number): Promise<IssueContext | null> {
  const { repoOwner, repoName } = config;

  // Get the workflow run to find associated issue
  const runJson = execCommand(
    `gh api repos/${repoOwner}/${repoName}/actions/runs/${runId} --jq '{head_branch: .head_branch, display_title: .display_title}'`
  );

  if (!runJson) return null;

  try {
    const run = JSON.parse(runJson);
    // Extract issue number from branch name (e.g., "agent/issue-123" or workflow title)
    const branchMatch = run.head_branch?.match(/issue[_-]?(\d+)/i);
    const titleMatch = run.display_title?.match(/#(\d+)/);
    const issueNumber = branchMatch?.[1] || titleMatch?.[1];

    if (!issueNumber) return null;

    const issueJson = execCommand(
      `gh issue view ${issueNumber} --repo ${repoOwner}/${repoName} --json number,title,body,labels,state`
    );

    if (!issueJson) return null;

    const issue = JSON.parse(issueJson);
    return {
      number: issue.number,
      title: issue.title,
      body: issue.body || '',
      labels: issue.labels?.map((l: { name: string }) => l.name) || [],
      state: issue.state?.toLowerCase() as 'open' | 'closed',
    };
  } catch {
    return null;
  }
}

// ============================================================================
// AI Analysis
// ============================================================================

export async function analyzeFailure(
  workflow: WorkflowRunContext,
  issue: IssueContext | null,
  logs: string
): Promise<FailureAnalysis> {
  const prompt = buildAnalysisPrompt(workflow, issue, logs);

  const messages: Array<{ role: 'user' | 'assistant'; content: string }> = [];
  let fullResponse = '';

  try {
    for await (const event of resilientQuery({
      queryParams: {
        prompt,
        options: {
          maxTurns: 1,
          allowedTools: [], // No tools, just analysis
        },
      },
      maxRetries: 3,
      baseDelayMs: 5000,
      log: console.log,
    })) {
      if (event.type === 'assistant' && event.message?.content) {
        for (const block of event.message.content) {
          if (block.type === 'text') {
            fullResponse += block.text;
          }
        }
      }
    }

    return parseAnalysisResponse(fullResponse);
  } catch (error) {
    console.error('AI analysis failed:', error);
    return {
      summary: 'AI analysis unavailable',
      rootCause: 'Could not complete AI analysis due to an error',
      recommendations: [
        {
          id: 1,
          action: 'manual_intervention',
          description: 'Review workflow logs manually',
          confidence: 'low',
        },
      ],
      canAutoResolve: false,
    };
  }
}

function buildAnalysisPrompt(
  workflow: WorkflowRunContext,
  issue: IssueContext | null,
  logs: string
): string {
  return `You are an expert DevOps engineer analyzing a failed GitHub Actions workflow.

## Workflow Information
- **Workflow**: ${workflow.workflowName}
- **Run ID**: ${workflow.runId}
- **Conclusion**: ${workflow.conclusion}
- **Attempt**: ${workflow.runAttempt}
- **URL**: ${workflow.htmlUrl}

${issue ? `## Issue Context
- **Issue #${issue.number}**: ${issue.title}
- **Labels**: ${issue.labels.join(', ') || 'none'}
- **State**: ${issue.state}

**Issue Body**:
${issue.body.slice(0, 2000)}${issue.body.length > 2000 ? '...(truncated)' : ''}
` : '## Issue Context\nNo associated issue found.'}

## Workflow Logs (last ${config.maxLogLines} lines)
\`\`\`
${logs}
\`\`\`

## Your Task
Analyze this workflow failure and provide:

1. **Summary**: A brief 1-2 sentence summary of what went wrong
2. **Root Cause**: The underlying cause of the failure
3. **Recommendations**: Specific actions to resolve this, with confidence levels

Format your response EXACTLY as follows (this will be parsed programmatically):

<analysis>
<summary>Your summary here</summary>
<root_cause>The root cause explanation</root_cause>
<recommendations>
<rec id="1" action="retry|fix_config|manual_intervention|skip|escalate" confidence="high|medium|low">
Description of what to do
<command>optional gh command to execute</command>
</rec>
<rec id="2" action="..." confidence="...">
...
</rec>
</recommendations>
<can_auto_resolve>true|false</can_auto_resolve>
</analysis>

Actions explained:
- retry: Simply re-run the workflow (transient failure)
- fix_config: Configuration or environment issue that needs fixing
- manual_intervention: Requires human decision or code changes
- skip: This task should be skipped/cancelled
- escalate: Needs attention from a senior engineer or maintainer`;
}

function parseAnalysisResponse(response: string): FailureAnalysis {
  // Extract content between tags
  const summaryMatch = response.match(/<summary>([\s\S]*?)<\/summary>/);
  const rootCauseMatch = response.match(/<root_cause>([\s\S]*?)<\/root_cause>/);
  const canAutoMatch = response.match(/<can_auto_resolve>(true|false)<\/can_auto_resolve>/i);

  const recommendations: Recommendation[] = [];
  const recMatches = response.matchAll(
    /<rec id="(\d+)" action="(\w+)" confidence="(\w+)">([\s\S]*?)(?:<command>([\s\S]*?)<\/command>)?\s*<\/rec>/g
  );

  for (const match of recMatches) {
    recommendations.push({
      id: parseInt(match[1], 10),
      action: match[2] as Recommendation['action'],
      confidence: match[3] as Recommendation['confidence'],
      description: match[4].trim(),
      command: match[5]?.trim(),
    });
  }

  // Fallback if parsing failed
  if (!summaryMatch && !rootCauseMatch && recommendations.length === 0) {
    return {
      summary: 'Analysis parsing failed - see raw response',
      rootCause: response.slice(0, 500),
      recommendations: [
        {
          id: 1,
          action: 'manual_intervention',
          description: 'Review the workflow logs manually',
          confidence: 'low',
        },
      ],
      canAutoResolve: false,
    };
  }

  return {
    summary: summaryMatch?.[1]?.trim() || 'Unable to extract summary',
    rootCause: rootCauseMatch?.[1]?.trim() || 'Unable to determine root cause',
    recommendations:
      recommendations.length > 0
        ? recommendations
        : [
            {
              id: 1,
              action: 'manual_intervention',
              description: 'No specific recommendations available',
              confidence: 'low',
            },
          ],
    canAutoResolve: canAutoMatch?.[1]?.toLowerCase() === 'true',
  };
}

// ============================================================================
// Comment Formatting
// ============================================================================

export function formatAnalysisComment(
  workflow: WorkflowRunContext,
  issue: IssueContext | null,
  analysis: FailureAnalysis
): string {
  const actionEmoji: Record<string, string> = {
    retry: '🔄',
    fix_config: '🔧',
    manual_intervention: '👤',
    skip: '⏭️',
    escalate: '🚨',
  };

  const confidenceEmoji: Record<string, string> = {
    high: '🟢',
    medium: '🟡',
    low: '🔴',
  };

  const recs = analysis.recommendations
    .map(
      (r) =>
        `${r.id}. ${actionEmoji[r.action] || '•'} **${r.action.replace('_', ' ')}** ${confidenceEmoji[r.confidence]} ${r.confidence}\n   ${r.description}${r.command ? `\n   \`${r.command}\`` : ''}`
    )
    .join('\n\n');

  return `## 🤖 PM Poll - Failure Analysis

**Workflow**: [${workflow.workflowName}](${workflow.htmlUrl})
**Conclusion**: \`${workflow.conclusion}\`
${issue ? `**Issue**: #${issue.number}` : ''}

### Summary
${analysis.summary}

### Root Cause
${analysis.rootCause}

### Recommendations
${recs}

---
${analysis.canAutoResolve ? '✅ **Auto-resolvable**: PM can attempt automatic resolution.' : '⚠️ **Manual intervention required**: Human input needed.'}

**Commands**:
- \`/pm-retry\` - Retry the failed workflow
- \`/pm-skip\` - Skip this task and continue
- \`/pm-action 1\` - Execute recommendation #1
`;
}

// ============================================================================
// Main Entry Point
// ============================================================================

export async function analyzePollFailure(
  workflowRun: WorkflowRunContext
): Promise<{ analysis: FailureAnalysis; comment: string } | null> {
  if (!isAnalyzerEnabled()) {
    console.log('PM Poll Analyzer is disabled');
    return null;
  }

  console.log(`Analyzing failed workflow: ${workflowRun.workflowName} (run ${workflowRun.runId})`);

  // Fetch logs
  const logs = await fetchWorkflowLogs(workflowRun.runId);
  console.log(`Fetched ${logs.split('\n').length} lines of logs`);

  // Fetch issue context
  const issue = await fetchIssueFromWorkflow(workflowRun.runId);
  if (issue) {
    console.log(`Found associated issue #${issue.number}: ${issue.title}`);
  } else {
    console.log('No associated issue found');
  }

  // Run AI analysis
  console.log('Running AI analysis...');
  const analysis = await analyzeFailure(workflowRun, issue, logs);

  // Format comment
  const comment = formatAnalysisComment(workflowRun, issue, analysis);

  return { analysis, comment };
}

// CLI entry point
if (require.main === module) {
  const args = process.argv.slice(2);
  if (args.length < 4) {
    console.error('Usage: ts-node pm-poll-analyzer.ts <runId> <workflowName> <conclusion> <htmlUrl>');
    process.exit(1);
  }

  const [runIdStr, workflowName, conclusion, htmlUrl] = args;
  const repoOwner = process.env.REPO_OWNER || '';
  const repoName = process.env.REPO_NAME || '';

  if (!repoOwner || !repoName) {
    console.error('REPO_OWNER and REPO_NAME environment variables are required');
    process.exit(1);
  }

  configurePollAnalyzer({ repoOwner, repoName });

  const workflowRun: WorkflowRunContext = {
    runId: parseInt(runIdStr, 10),
    workflowName,
    conclusion: conclusion as WorkflowRunContext['conclusion'],
    htmlUrl,
    headSha: '',
    runAttempt: 1,
  };

  analyzePollFailure(workflowRun)
    .then((result) => {
      if (result) {
        console.log('\n=== Analysis Result ===\n');
        console.log(result.comment);
        // Output JSON for workflow to consume
        console.log('\n=== JSON Output ===');
        console.log(JSON.stringify(result.analysis, null, 2));
      }
    })
    .catch((err) => {
      console.error('Analysis failed:', err);
      process.exit(1);
    });
}
