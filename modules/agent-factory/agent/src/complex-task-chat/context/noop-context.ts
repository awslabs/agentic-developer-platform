/**
 * NoopContextManager — stub implementation that compiles but does nothing.
 *
 * Used as a default when context management is disabled or during testing.
 */
import { ContextManager, SDKMessage, AssemblyMeta, AgentTool } from './types';

export class NoopContextManager implements ContextManager {
  async assemble(input: {
    sessionId: string;
    userMessage: string;
    tokenBudget: number;
  }): Promise<{ messages: SDKMessage[]; meta: AssemblyMeta }> {
    return {
      messages: [],
      meta: {
        rawMessageCount: 0,
        summaryCount: 0,
        estimatedTokens: 0,
        compactionTriggered: false,
      },
    };
  }

  async record(_input: {
    sessionId: string;
    userMessage: SDKMessage;
    assistantMessage: SDKMessage;
  }): Promise<void> {
    // no-op
  }

  async assertOwnership(_sessionId: string, _userId: string, _tenantId?: string, _identity?: {
    orgId?: string;
    teamId?: string;
    departmentId?: string;
    accountType?: string;
  }): Promise<void> {
    // no-op — always passes
  }

  tools(): AgentTool[] {
    return [];
  }
}
