import { z } from 'zod';
import { GatewayArtifactStore } from './artifacts/gateway-artifact-store';
import { GatewayContextManager } from './context/gateway-context';
import { GatewaySummarizer } from './context/summarize/gateway-summarizer';
import { AgentTool, AgentToolResult } from './context/types';
import { ChatDataClient, ChatDataError } from './gateway/chat-data-client';
import { GatewayMemoryProvider } from './memory/gateway-memory';

type Scope = Awaited<ReturnType<ChatDataClient['sessionScope']>>;
type AcceptedTurn = Awaited<ReturnType<ChatDataClient['nextTurn']>>;

export class SandboxDataRuntime {
  private readonly context: GatewayContextManager;
  private readonly memory: GatewayMemoryProvider;
  private readonly artifacts: GatewayArtifactStore;
  private readonly registered: Map<string, AgentTool>;
  private readonly scope: Scope;
  private readonly turn: AcceptedTurn;

  constructor(private readonly client: ChatDataClient, scope: Scope, turn: AcceptedTurn, workspaceRoot = '/tmp/workspace') {
    this.scope = { ...scope };
    this.turn = structuredClone(turn);
    this.context = new GatewayContextManager(client, new GatewaySummarizer(client));
    this.memory = new GatewayMemoryProvider(client);
    this.artifacts = new GatewayArtifactStore(client, scope.session_id, workspaceRoot);
    this.registered = new Map([
      ...this.context.tools(), ...this.memory.tools(), ...this.artifacts.toolsForTurn({ sessionId: scope.session_id }),
    ].map(tool => [tool.name, tool]));
  }

  async prepare(tokenBudget = 12_000, memoryTokenBudget = 2000) {
    if (!Number.isSafeInteger(tokenBudget) || tokenBudget < 1 || !Number.isSafeInteger(memoryTokenBudget) ||
      memoryTokenBudget < 0 || memoryTokenBudget > tokenBudget) throw new ChatDataError('invalid_request');
    const current = await this.client.sessionScope();
    if (current.run_id !== this.scope.run_id || current.session_id !== this.scope.session_id) throw new ChatDataError('scope_mismatch');
    const memories = await this.memory.retrieve({ query: this.turn.message.content.slice(0, 1024), tokenBudget: memoryTokenBudget, limit: 10 });
    const memoryTokens = memories.reduce((sum, memory) => sum + Math.ceil(memory.content.length / 4), 0);
    const history = await this.context.assemble({
      sessionId: this.scope.session_id, userMessage: this.turn.message.content, tokenBudget: tokenBudget - memoryTokens,
    });
    const attachments: Array<{ id: string; path: string }> = [];
    for (const part of this.turn.message.parts) {
      const destination = `attachments/${part.artifactId}`;
      await this.artifacts.fetch(part.artifactId, destination, this.scope.session_id);
      attachments.push({ id: part.artifactId, path: destination });
    }
    return { ...history, memories, attachments, userMessage: this.turn.message.content };
  }

  tools(): AgentTool[] {
    return [...this.registered.values()].map(tool => ({ ...tool, handler: input => this.executeTool(tool.name, input) }));
  }

  async executeTool(name: string, input: Record<string, unknown>): Promise<AgentToolResult> {
    const tool = this.registered.get(name);
    if (!tool) throw new ChatDataError('invalid_request');
    const parsed = z.object(tool.inputSchema).strict().safeParse(input);
    if (!parsed.success) throw new ChatDataError('invalid_request');
    return tool.handler(parsed.data);
  }

  async record(assistantContent: string): Promise<void> {
    const messageId = await this.context.recordReply({
      sessionId: this.scope.session_id,
      userMessage: { role: 'user', content: this.turn.message.content },
      assistantMessage: { role: 'assistant', content: assistantContent },
    });
    await this.client.submitTurnResult({ outcome: 'completed', message_id: messageId });
  }
}
