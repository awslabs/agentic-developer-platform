import { IssueContext, Plan, PlanStep } from '../types';
import { Logger } from './Logger';
import { resilientQuery } from '../utils/resilientQuery';
import { wrapUntrusted } from '../utils/trust-boundary';

/**
 * Planning agent for MCP server onboarding.
 * Uses the query API (not session) to enable Skills, WebSearch, WebFetch.
 * The onboard-mcp-server skill in .claude/skills/ guides the workflow.
 */
export class MCPOnboardPlanningAgent {
  private logger: Logger;

  constructor(logger: Logger) {
    this.logger = logger;
  }

  async generatePlan(issueContext: IssueContext, workDir: string): Promise<Plan> {
    this.logger.info('Starting MCP onboarding plan generation with skills', {
      component: 'MCPOnboardPlanningAgent',
      issueNumber: issueContext.issueNumber,
    });

    console.log('\n📋 Generating MCP onboarding plan (with skills enabled)...');
    console.log(`   Issue: #${issueContext.issueNumber} - ${issueContext.issueTitle}`);

    const prompt = this.buildPrompt(issueContext);

    try {
      let response = '';
      let turnCount = 0;

      for await (const message of resilientQuery({
        queryParams: {
          prompt,
          options: {
            model: process.env.ANTHROPIC_MODEL || 'claude-sonnet-4-5-20250929',
            cwd: workDir,
            allowedTools: ['Read', 'Write', 'Edit', 'Glob', 'Grep', 'Bash', 'WebSearch', 'WebFetch', 'Skill'],
            settingSources: ['project'],
            permissionMode: 'bypassPermissions',
            maxTurns: 100,
          }
        },
        maxRetries: 3,
        baseDelayMs: 5_000,
        log: (msg) => {
          console.log(msg);
          this.logger.info(msg, { component: 'MCPOnboardPlanningAgent' });
        },
      })) {
        if (message.type === 'assistant') {
          turnCount++;
          for (const block of message.message.content) {
            if ('text' in block && typeof block.text === 'string') {
              response += block.text;
            }
            if ('name' in block) {
              const toolName = (block as { name: string }).name;
              console.log(`   🔧 Tool: ${toolName}`);
              if (toolName === 'Skill') {
                console.log('   ✅ Skill invoked!');
              }
            }
          }
        }
      }

      console.log(`\n✅ MCP onboarding plan generated (${turnCount} turns)`);

      const plan = this.parsePlanResponse(response);
      this.logger.info('MCP onboarding plan generated', {
        component: 'MCPOnboardPlanningAgent',
        steps: plan.steps.length,
        turns: turnCount,
      });
      return plan;
    } catch (err) {
      this.logger.error('MCP onboarding plan generation failed', err as Error, {
        component: 'MCPOnboardPlanningAgent',
      });
      throw err;
    }
  }

  private buildPrompt(issueContext: IssueContext): string {
    return `You are an MCP server onboarding agent. Your task is to fully onboard and deploy a new MCP server to the BedrockGateway platform.

## GitHub Issue #${issueContext.issueNumber}
**Title:** ${issueContext.issueTitle}

**Description:**
${wrapUntrusted(issueContext.issueBody)}

## Instructions
Read the skill file at .claude/skills/onboard-mcp-server/SKILL.md — it contains the complete workflow you must follow.

Execute ALL stages from the skill during this planning phase:
- Stage 0: Resolve server identity (web search if needed)
- Stage 1: Research the MCP server repo
- Stage 2: Generate catalogue.json
- Stage 3: Test MCP protocol with Docker (if available)

Then include ALL remaining stages in your plan for the code generation phase:
- Stage 5: Deploy to EKS (kubectl create namespace, kubectl apply Deployment + Service, wait for Ready, verify MCP protocol via port-forward)
- Stage 6: Save K8s manifests and catalogue.json to repo, create PR

Your JSON plan MUST include the K8s deployment steps. Example:
\`\`\`json
{
  "summary": "Onboard and deploy [Server Name] MCP server",
  "steps": [
    { "description": "Research complete: [findings summary]" },
    { "description": "Create mcp-servers namespace if not exists: kubectl create namespace mcp-servers --dry-run=client -o yaml | kubectl apply -f -" },
    { "description": "Deploy K8s Deployment for [server-name] using image [image] on port [port] in mcp-servers namespace" },
    { "description": "Deploy K8s ClusterIP Service for [server-name] in mcp-servers namespace" },
    { "description": "Wait for pod ready: kubectl wait --for=condition=ready pod -l app=[server-name] -n mcp-servers --timeout=120s" },
    { "description": "Verify MCP protocol via port-forward: kubectl port-forward, then curl initialize + tools/list" },
    { "description": "Save catalogue.json and K8s manifests (deployment.yaml, service.yaml) to mcp-servers/catalogue/[server-name]/" },
    { "description": "Create PR with all files" }
  ],
  "estimatedFiles": [
    "mcp-servers/catalogue/[name]/catalogue.json",
    "mcp-servers/catalogue/[name]/deployment.yaml",
    "mcp-servers/catalogue/[name]/service.yaml"
  ]
}
\`\`\`

CRITICAL: The plan MUST include kubectl deployment steps. Do not skip the EKS deployment.
CRITICAL: Read the full SKILL.md file first — it has all the details including kubectl commands.`;
  }

  private parsePlanResponse(response: string): Plan {
    try {
      const jsonMatch = response.match(/```json\s*([\s\S]*?)\s*```/) ||
                        response.match(/```\s*([\s\S]*?)\s*```/) ||
                        [null, response];

      const jsonStr = jsonMatch[1] || response;
      const parsed = JSON.parse(jsonStr.trim());

      return {
        summary: parsed.summary || 'MCP server onboarding',
        steps: (parsed.steps || []).map((step: { description?: string }) => ({
          description: step.description || 'Step',
          completed: false,
        })),
        estimatedFiles: parsed.estimatedFiles || [],
      };
    } catch {
      this.logger.warn('Failed to parse MCP onboarding plan, using fallback', {
        component: 'MCPOnboardPlanningAgent',
      });

      return {
        summary: 'MCP server onboarding (from skill output)',
        steps: [{
          description: response.substring(0, 500),
          completed: false,
        }],
        estimatedFiles: [],
      };
    }
  }

  formatPlanComment(plan: Plan): string {
    let comment = `## 🤖 MCP Onboarding Plan\n\n`;
    comment += `**Summary:** ${plan.summary}\n\n`;

    comment += `### Steps\n`;
    plan.steps.forEach((step: PlanStep, index: number) => {
      comment += `${index + 1}. ${step.description}\n`;
    });

    if (plan.estimatedFiles.length > 0) {
      comment += `\n### Files to Create\n`;
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
