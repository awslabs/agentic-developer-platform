import { query } from '@anthropic-ai/claude-agent-sdk';
import * as fs from 'fs';
import { Plan, CodeResult, Milestone, IssueContext } from '../types';
import { Logger } from './Logger';
import { ProgressTracker } from './ProgressTracker';
import { wrapUntrusted } from '../utils/trust-boundary';

export class CodeGenerationAgent {
  private logger: Logger;
  private progressTracker: ProgressTracker;

  constructor(logger: Logger, progressTracker: ProgressTracker) {
    this.logger = logger;
    this.progressTracker = progressTracker;
  }

  async executePlan(plan: Plan, workDir: string, issueNumber: number, issueContext?: IssueContext): Promise<CodeResult> {
    const projectDir = `${workDir}/projects/issue-${issueNumber}`;
    this.logger.info('Starting code generation', { component: 'CodeGenerationAgent', steps: plan.steps.length, projectDir });

    const result: CodeResult = { success: false, filesModified: [], filesCreated: [] };

    try {
      const prompt = this.buildCodeGenPrompt(plan, projectDir, issueContext);
      
      // Use the V1 query API with built-in file tools
      console.log('\n🤖 Starting Claude code generation...\n');
      let turnCount = 0;
      
      const session = query({
        prompt,
        options: {
          model: process.env.ANTHROPIC_MODEL || 'claude-sonnet-4-5-20250929',
          allowedTools: ['Read', 'Write', 'Edit', 'Glob', 'Grep', 'Bash', 'WebSearch', 'WebFetch', 'Task'],
          permissionMode: 'bypassPermissions',
          maxTurns: 2000,
        }
      });

      try {
        for await (const message of session) {
          if (message.type === 'assistant') {
            turnCount++;
            console.log(`\n--- Turn ${turnCount} ---`);

            // Log tool usage with details
            for (const block of message.message.content) {
              if ('name' in block) {
                const toolName = block.name;
                this.logger.info('Tool used', { component: 'CodeGenerationAgent', tool: toolName });

                // Stream detailed tool info to console
                if ('input' in block) {
                  const input = block.input as Record<string, unknown>;
                  if (toolName === 'Write') {
                    console.log(`📝 Write: ${input.file_path}`);
                    result.filesCreated.push(input.file_path as string);
                  } else if (toolName === 'Edit') {
                    console.log(`✏️  Edit: ${input.file_path}`);
                    result.filesModified.push(input.file_path as string);
                  } else if (toolName === 'Read') {
                    console.log(`📖 Read: ${input.file_path}`);
                  } else if (toolName === 'Bash') {
                    const cmd = (input.command as string || '').substring(0, 100);
                    console.log(`💻 Bash: ${cmd}${cmd.length >= 100 ? '...' : ''}`);
                  } else if (toolName === 'WebSearch') {
                    console.log(`🔍 WebSearch: ${input.query}`);
                  } else if (toolName === 'WebFetch') {
                    console.log(`🌐 WebFetch: ${input.url}`);
                  } else if (toolName === 'Glob') {
                    console.log(`📂 Glob: ${input.pattern}`);
                  } else if (toolName === 'Grep') {
                    console.log(`🔎 Grep: ${input.pattern}`);
                  } else if (toolName === 'Task') {
                    console.log(`📋 Task: Delegating subtask`);
                  } else {
                    console.log(`🔧 ${toolName}`);
                  }
                }
              }
              if ('text' in block && block.text) {
                // Show thinking/reasoning (truncated)
                const text = block.text.substring(0, 300);
                console.log(`💭 ${text}${block.text.length > 300 ? '...' : ''}`);
              }
            }
          }

          if (message.type === 'result') {
            if (message.subtype === 'success') {
              result.success = true;
              console.log(`\n✅ Code generation completed successfully!`);
              console.log(`💰 Total cost: ${message.total_cost_usd?.toFixed(4) || 'N/A'}`);
              console.log(`🔄 Total turns: ${turnCount}`);
              this.logger.info('Code generation succeeded', { 
                component: 'CodeGenerationAgent', 
                cost: message.total_cost_usd,
                turns: turnCount
              });
            } else {
              result.error = `Code generation ended with: ${message.subtype}`;
              console.log(`\n⚠️  Code generation ended: ${message.subtype}`);
              this.logger.warn('Code generation ended', { component: 'CodeGenerationAgent', subtype: message.subtype });
            }
          }
        }
      } finally {
        // Close the query to terminate the underlying Claude Code process.
        // Without this, the async generator never terminates and the process hangs
        // indefinitely (same pattern as PlanningAgent's session.close()).
        session.close();
      }

      // Update progress for each step
      for (let i = 0; i < plan.steps.length; i++) {
        await this.progressTracker.updateChecklistItem(i + 1, true);
        await this.onMilestone({
          name: `Step ${i + 1} Complete`,
          description: plan.steps[i].description,
          timestamp: new Date().toISOString(),
        });
      }

      this.logger.info('Code generation complete', { 
        component: 'CodeGenerationAgent', 
        filesCreated: result.filesCreated.length,
        filesModified: result.filesModified.length 
      });

      // Check if agent wrote a blocker file
      const blockerFile = `${projectDir}/BLOCKER.md`;
      if (fs.existsSync(blockerFile)) {
        const blockerContent = fs.readFileSync(blockerFile, 'utf-8');
        result.error = blockerContent;
        result.success = false;
        this.logger.warn('Agent reported a blocker', { component: 'CodeGenerationAgent' });
      }
    } catch (err) {
      result.error = (err as Error).message;
      this.logger.error('Code generation failed', err as Error, { component: 'CodeGenerationAgent' });
    }

    return result;
  }

  private buildCodeGenPrompt(plan: Plan, projectDir: string, issueContext?: IssueContext): string {
    const steps = plan.steps.map((s, i) => `${i + 1}. ${s.description}`).join('\n');
    const repoDir = projectDir.split('/projects/')[0];
    
    const issueSection = issueContext ? `
## ORIGINAL ISSUE CONTEXT
**Issue #${issueContext.issueNumber}: ${issueContext.issueTitle}**

${wrapUntrusted(issueContext.issueBody)}

---
The above is the original issue body for reference. The plan below is the approved implementation. Follow the plan.
` : '';

    return `You are an AI agent that EXECUTES tasks on a real AWS environment. You have full Bash access and can install tools, run commands, and interact with AWS services.
${issueSection}

## CRITICAL: You MUST actually execute commands, not simulate them!
- You are running on an EC2 instance with AWS IAM permissions
- You have sudo access to install packages
- You MUST use the Bash tool to run real commands
- NEVER write "simulated" results - actually run the commands and capture real output
- If a tool is missing (terraform, aws cli, python packages), INSTALL IT using Bash

## Environment Setup
If tools are missing, install them:
- Terraform: sudo yum install -y yum-utils && sudo yum-config-manager --add-repo https://rpm.releases.hashicorp.com/AmazonLinux/hashicorp.repo && sudo yum -y install terraform
- AWS CLI: should be pre-installed, if not: curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip" && unzip awscliv2.zip && sudo ./aws/install
- Python packages: pip3 install boto3 requests pandas numpy scikit-learn

## Repository Directory
The full repository is cloned at: ${repoDir}
Existing code from previous issues is available there (e.g., ${repoDir}/projects/issue-15/, ${repoDir}/projects/issue-24/).

## Project Output Directory
All NEW files you create should go in: ${projectDir}
Use ABSOLUTE PATHS for all file operations.

## Plan Summary
${plan.summary}

## Steps to Implement
${steps}

## Files to Create/Modify
${plan.estimatedFiles.map(f => `${projectDir}/${f}`).join(', ')}

## MANDATORY AWS RESOURCE NAMING CONVENTION
You MUST use these EXACT prefixes for ALL AWS resources you create. The IAM role only has permissions for these patterns:

- S3 Buckets: MUST start with one of: \`ai-security-\`, \`vuln-exp-\`, \`experiment-\`, \`ml-security-demo-\`, \`ml-security-experiment-\`, \`ml-data-\`
- IAM Roles: MUST start with one of: \`ai-security-\`, \`vuln-exp-\`, \`experiment-\`, \`ml-security-demo-\`, \`ml-security-experiment-\`
- SageMaker resources: Any name is allowed, but associated IAM roles must follow the pattern above

**EXAMPLES:**
- GOOD: \`ml-security-experiment-bucket-12345\`, \`ai-security-sagemaker-role\`
- BAD: \`vuln-exploit-pred-bucket\`, \`my-custom-bucket\`, \`training-data-bucket\`

If you create terraform or AWS resources with names that don't match these patterns, you WILL get permission denied errors.

## Execution Rules
1. First, create the project directory: mkdir -p ${projectDir}
2. Check what tools are available (terraform, aws, python3, pip3)
3. INSTALL any missing tools using Bash
4. ACTUALLY RUN every command (terraform init, terraform apply, aws commands, python scripts, etc.)
5. Capture REAL output from commands and include in documentation
6. If running terraform: use -auto-approve flag for apply and destroy
7. ALWAYS clean up resources (terraform destroy) when done, even if experiment fails
8. Document real results, real errors, real metrics - never fabricate or simulate
9. Use the Read tool to examine existing files in the repository before creating new ones
10. AWS Region is us-east-1
11. VERIFY resource names match the MANDATORY NAMING CONVENTION above before creating them
12. NEVER leave background processes running (no trailing &, no tail -f, no background sky commands). When monitoring logs or service status, use a bounded command (e.g., \`sky serve status\`, \`tail -n 50\`, \`timeout 30 tail -f\`) instead of open-ended ones. Background processes prevent the agent from exiting cleanly.

## CRITICAL FAILURE HANDLING RULES
- If a command fails with a PERMISSION ERROR: STOP and document the exact error. Do NOT try workarounds.
- If terraform apply fails: Run terraform destroy to clean up partial resources, then STOP and document the error.
- NEVER generate synthetic/fake data as a substitute for real data downloads.
- NEVER run training locally as a substitute for SageMaker when SageMaker was requested.
- NEVER improvise alternative approaches when the requested approach fails.
- If you cannot complete a step as specified, STOP and create documentation explaining exactly what failed and why.
- Do NOT try to "complete" the task by doing something different from what was asked.

## What to do when blocked
1. Document the exact error message
2. Clean up any partially created resources (terraform destroy)
3. Write an execution_log.md with what succeeded and what failed
4. Write a file called BLOCKER.md in the project directory with:
   - The exact error message
   - Which step failed
   - What permissions or fixes are needed
   - Any resources that were partially created and need manual cleanup
   This file will be automatically posted as a comment on the GitHub issue for the human to review.
5. STOP - do not invent alternative approaches

Now execute the plan. Start by checking available tools and installing what's needed.`;
  }

  async onMilestone(milestone: Milestone): Promise<void> {
    await this.progressTracker.postMilestoneComment(milestone);
  }
}
