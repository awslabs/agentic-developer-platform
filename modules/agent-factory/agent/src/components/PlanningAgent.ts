import { execSync } from 'child_process';
import { IssueContext, Plan, PlanStep } from '../types';
import { Logger } from './Logger';
import { resilientQuery, SDKStreamMessage } from '../utils/resilientQuery';

interface ContentBlock {
  type: string;
  text?: string;
}

export class PlanningAgent {
  private logger: Logger;

  constructor(logger: Logger) {
    this.logger = logger;
  }

  async generatePlan(issueContext: IssueContext, workDir: string, feedback?: string, previousPlan?: Plan): Promise<Plan> {
    const isRevision = !!feedback && !!previousPlan;
    this.logger.info(isRevision ? 'Revising plan based on feedback' : 'Starting plan generation', { 
      component: 'PlanningAgent', 
      issueNumber: issueContext.issueNumber,
      isRevision
    });
    
    if (isRevision) {
      console.log('\n🔄 Revising plan based on feedback...');
      console.log(`   Feedback: ${feedback?.substring(0, 100)}${(feedback?.length || 0) > 100 ? '...' : ''}`);
    } else {
      console.log('\n📋 Generating implementation plan...');
      console.log(`   Issue: #${issueContext.issueNumber} - ${issueContext.issueTitle}`);
    }

    // Scan repository structure to provide context
    const repoContext = this.scanRepository(workDir);
    console.log(`   Repository scanned: ${repoContext.fileCount} files found`);

    const prompt = isRevision 
      ? this.buildRevisionPrompt(issueContext, feedback!, previousPlan!, repoContext)
      : this.buildPlanningPrompt(issueContext, repoContext);
    
    let response = '';
    for await (const msg of resilientQuery({
      queryParams: {
        prompt,
        options: {
          model: process.env.ANTHROPIC_MODEL || 'claude-sonnet-4-5-20250929',
          maxTurns: 1,
          permissionMode: 'plan',
        },
      },
      maxRetries: 3,
      log: (logMsg) => this.logger.info(logMsg, { component: 'PlanningAgent' }),
    })) {
      if (msg.type === 'assistant') {
        response += this.extractText(msg);
      }
    }

    const plan = this.parsePlanResponse(response);
    console.log(`\n✅ Plan ${isRevision ? 'revised' : 'generated'} with ${plan.steps.length} steps:`);
    plan.steps.forEach((step, i) => {
      console.log(`   ${i + 1}. ${step.description.substring(0, 80)}${step.description.length > 80 ? '...' : ''}`);
    });
    console.log(`\n📁 Files to create/modify: ${plan.estimatedFiles.join(', ') || 'TBD'}\n`);

    this.logger.info('Plan generated', { component: 'PlanningAgent', steps: plan.steps.length, isRevision });
    return plan;
  }

  private scanRepository(workDir: string): { structure: string; fileCount: number } {
    try {
      this.logger.info(`Scanning repository at: ${workDir}`, { component: 'PlanningAgent' });
      
      // Get full file listing
      const allFiles = execSync(
        `find . -type f | grep -v node_modules | grep -v '.git/' | sort`,
        { cwd: workDir, encoding: 'utf-8', timeout: 30000 }
      ).trim();
      
      const files = allFiles.split('\n').filter(f => f.length > 0);
      this.logger.info(`Found ${files.length} total files`, { component: 'PlanningAgent' });
      
      return {
        structure: allFiles,
        fileCount: files.length
      };
    } catch (err) {
      const errorMsg = (err as Error).message;
      this.logger.error(`Scan error: ${errorMsg}`, err as Error, { component: 'PlanningAgent' });
      return { structure: `Could not scan repository: ${errorMsg}`, fileCount: 0 };
    }
  }

  private buildPlanningPrompt(issueContext: IssueContext, repoContext: { structure: string; fileCount: number }): string {
    const retrySection = issueContext.retryGuidance ? `
## RETRY GUIDANCE (from user after previous failure)
The user has provided additional guidance after a previous attempt failed:

${issueContext.retryGuidance}

Take this guidance into account when creating your plan.
` : '';

    return `You are a software development planning agent. Your task is to analyze a GitHub issue and create a detailed implementation plan.

## Repository Structure
The repository has been scanned and contains ${repoContext.fileCount} relevant files:

${repoContext.structure}

## GitHub Issue #${issueContext.issueNumber}
**Title:** ${issueContext.issueTitle}

**Description:**
${issueContext.issueBody}
${retrySection}
## Your Task
Create a detailed implementation plan for this issue. The plan should:
1. Break down the work into clear, actionable steps
2. Identify which files need to be created or modified
3. Consider any dependencies between steps
4. Be specific about what each step accomplishes

## CRITICAL RULES - READ CAREFULLY:
1. The repository structure above is the ACTUAL output of scanning the cloned repository
2. If a file path appears in the structure above, IT EXISTS - do not claim otherwise
3. The working directory for execution will be the repository root where all these files are accessible
4. DO NOT create BLOCKER entries for "missing files" if those files appear in the repository structure
5. Trust the scan results - they are accurate
6. If the issue mentions paths like "projects/issue-24/terraform/", search for them in the structure above before claiming they don't exist

## Response Format
Respond with a JSON object in this exact format:
\`\`\`json
{
  "summary": "Brief summary of what will be implemented",
  "steps": [
    {
      "description": "Detailed description of step 1"
    },
    {
      "description": "Detailed description of step 2"
    }
  ],
  "estimatedFiles": ["file1.ts", "file2.py"]
}
\`\`\`

Do NOT include blockers for missing files if those files exist in the repository structure above.`;
  }

  private buildRevisionPrompt(
    issueContext: IssueContext, 
    feedback: string, 
    previousPlan: Plan,
    repoContext: { structure: string; fileCount: number }
  ): string {
    return `You are a software development planning agent. Your previous plan was rejected and you need to revise it based on feedback.

## Repository Structure
The repository has been scanned and contains ${repoContext.fileCount} relevant files:

${repoContext.structure}

## GitHub Issue #${issueContext.issueNumber}
**Title:** ${issueContext.issueTitle}

**Description:**
${issueContext.issueBody}

## Previous Plan
${JSON.stringify(previousPlan, null, 2)}

## Rejection Feedback
${feedback}

## Your Task
Revise the implementation plan based on the feedback. Address all concerns raised in the feedback.

IMPORTANT: 
- If the issue mentions existing files or directories (like projects/issue-15/remediated_code/), USE THEM - they exist in the repository as shown above.
- Do NOT claim files don't exist if they appear in the repository structure above.
- Review the existing code before proposing to create new files.

## Response Format
Respond with a JSON object in this exact format:
\`\`\`json
{
  "summary": "Brief summary of what will be implemented",
  "steps": [
    {
      "description": "Detailed description of step 1"
    }
  ],
  "estimatedFiles": ["file1.ts", "file2.py"]
}
\`\`\`

Do NOT include blockers unless there are genuine technical blockers.`;
  }

  private extractText(msg: SDKStreamMessage): string {
    if (msg.type === 'assistant' && msg.message?.content) {
      return (msg.message.content as ContentBlock[])
        .filter((block: ContentBlock): block is ContentBlock & { text: string } => block.type === 'text' && typeof block.text === 'string')
        .map((block: ContentBlock & { text: string }) => block.text)
        .join('');
    }
    return '';
  }

  private parsePlanResponse(response: string): Plan {
    try {
      // Extract JSON from response (may be wrapped in markdown code blocks)
      const jsonMatch = response.match(/```json\s*([\s\S]*?)\s*```/) || 
                        response.match(/```\s*([\s\S]*?)\s*```/) ||
                        [null, response];
      
      const jsonStr = jsonMatch[1] || response;
      const parsed = JSON.parse(jsonStr.trim());
      
      return {
        summary: parsed.summary || 'Implementation plan',
        steps: (parsed.steps || []).map((step: { description?: string }) => ({
          description: step.description || 'Step',
          completed: false,
        })),
        estimatedFiles: parsed.estimatedFiles || [],
      };
    } catch (err) {
      this.logger.warn('Failed to parse plan response, using fallback', { 
        component: 'PlanningAgent', 
        error: (err as Error).message 
      });
      
      // Fallback: create a basic plan from the response
      return {
        summary: 'Implementation plan (parsed from response)',
        steps: [{
          description: response.substring(0, 500),
          completed: false,
        }],
        estimatedFiles: [],
      };
    }
  }

  formatPlanComment(plan: Plan): string {
    let comment = `## 🤖 Implementation Plan\n\n`;
    comment += `**Summary:** ${plan.summary}\n\n`;
    
    comment += `### Steps\n`;
    plan.steps.forEach((step: PlanStep, index: number) => {
      comment += `${index + 1}. ${step.description}\n`;
    });
    
    if (plan.estimatedFiles.length > 0) {
      comment += `\n### Files to Create/Modify\n`;
      plan.estimatedFiles.forEach((file: string) => {
        comment += `- \`${file}\`\n`;
      });
    }
    
    comment += `\n---\n`;
    comment += `**To approve this plan:** Comment \`/approve\`\n`;
    comment += `**To request changes:** Comment \`/reject <your feedback>\`\n`;
    
    return comment;
  }
}
