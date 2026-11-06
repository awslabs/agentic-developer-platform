import { z } from 'zod';
import { GatewayArtifactStore } from './artifacts/gateway-artifact-store';
import { GatewayContextManager } from './context/gateway-context';
import { GatewaySummarizer } from './context/summarize/gateway-summarizer';
import { AgentTool, AgentToolResult } from './context/types';
import { GatewayDraftStore } from './draft/gateway-draft-store';
import { draftToolsForTurn } from './draft/tools';
import { AnswerEvidence } from './gateway/answer-evidence';
import { ChatDataClient, ChatDataError } from './gateway/chat-data-client';
import { GatewayMemoryProvider } from './memory/gateway-memory';
import { EvidenceCollector, platformTools, toolListingSchema } from './platform/tools';

type Scope = Awaited<ReturnType<ChatDataClient['sessionScope']>>;
type AcceptedTurn = Awaited<ReturnType<ChatDataClient['nextTurn']>>;

export class SandboxDataRuntime {
  private readonly context: GatewayContextManager;
  private readonly memory: GatewayMemoryProvider;
  private readonly artifacts: GatewayArtifactStore;
  private readonly registered: Map<string, AgentTool>;
  private readonly memoryTools: string[];
  private readonly artifactTools: string[];
  private readonly evidence = new EvidenceCollector();
  private platformToolNames: string[] = [];
  private readonly scope: Scope;
  private readonly turn: AcceptedTurn;

  constructor(private readonly client: ChatDataClient, scope: Scope, turn: AcceptedTurn, workspaceRoot = '/tmp/workspace') {
    this.scope = { ...scope };
    this.turn = structuredClone(turn);
    this.context = new GatewayContextManager(client, new GatewaySummarizer(client));
    this.memory = new GatewayMemoryProvider(client);
    this.artifacts = new GatewayArtifactStore(client, scope.session_id, workspaceRoot);
    const memoryTools = this.memory.tools();
    const artifactTools = this.artifacts.toolsForTurn({ sessionId: scope.session_id });
    this.memoryTools = memoryTools.map(tool => tool.name);
    this.artifactTools = artifactTools.map(tool => tool.name);
    // The intent-refinement persona the gateway renders into the prompt expects `update_draft`
    // (#8113); the gateway draft store already serves this session, so the tool is provided
    // rather than its instructions stripped.
    const draftTools = draftToolsForTurn(new GatewayDraftStore(client, scope.session_id), { sessionId: scope.session_id });
    this.registered = new Map([...this.context.tools(), ...memoryTools, ...artifactTools, ...draftTools].map(tool => [tool.name, tool]));
  }

  /**
   * First-party platform tools (#8113): the gateway lists only what the owner's tenant
   * entitles and the owner consented to (tools layer, #7245), evaluated for this run's
   * principal. A gateway without the listing route, or one that refuses it, leaves the
   * turn without platform tools rather than with tools that cannot work; a broken
   * capability still fails the turn.
   */
  private async registerPlatformTools(): Promise<void> {
    let raw: unknown;
    try {
      raw = await this.client.runRequest('tools/list');
    } catch (error) {
      if (!(error instanceof ChatDataError) || error.code === 'expired' || error.reason !== undefined) throw error;
      console.error(`[sandbox] platform tools unavailable on this gateway (${error.code}); continuing without them`);
      return;
    }
    const listing = toolListingSchema.safeParse(raw);
    if (!listing.success) throw new ChatDataError('invalid_response');
    for (const tool of platformTools(this.client, listing.data, this.evidence)) {
      if (this.registered.has(tool.name)) throw new ChatDataError('invalid_response');
      this.registered.set(tool.name, tool);
      this.platformToolNames.push(tool.name);
    }
    if (listing.data.withheld.length) {
      console.error(`[sandbox] platform tools withheld: ${listing.data.withheld.map(entry => `${entry.name} (${entry.reason})`).join(', ')}`);
    }
  }

  /**
   * Memory and artifacts are optional stores (#8028): once the gateway has answered
   * `chat_memory_unconfigured` / `chat_artifacts_unconfigured`, that store's tools are
   * withdrawn from the turn so the model is never offered a tool that cannot work.
   */
  private withdrawUnconfiguredTools(): void {
    for (const name of [...(this.memory.unconfigured ? this.memoryTools : []), ...(this.artifacts.unconfigured ? this.artifactTools : [])]) {
      this.registered.delete(name);
    }
  }

  async prepare(tokenBudget = 12_000, memoryTokenBudget = 2000) {
    if (!Number.isSafeInteger(tokenBudget) || tokenBudget < 1 || !Number.isSafeInteger(memoryTokenBudget) ||
      memoryTokenBudget < 0 || memoryTokenBudget > tokenBudget) throw new ChatDataError('invalid_request');
    const current = await this.client.sessionScope();
    if (current.run_id !== this.scope.run_id || current.session_id !== this.scope.session_id) throw new ChatDataError('scope_mismatch');
    // An unconfigured memory store yields no memories (and no memory tools below); any other
    // memory failure still fails preparation.
    const memories = await this.memory.retrieve({ query: this.turn.message.content.slice(0, 1024), tokenBudget: memoryTokenBudget, limit: 10 });
    const memoryTokens = memories.reduce((sum, memory) => sum + Math.ceil(memory.content.length / 4), 0);
    const history = await this.context.assemble({
      sessionId: this.scope.session_id, userMessage: this.turn.message.content, tokenBudget: tokenBudget - memoryTokens,
    });
    const attachments: Array<{ id: string; path: string }> = [];
    for (const part of this.turn.message.parts) {
      const destination = `attachments/${part.artifactId}`;
      try {
        await this.artifacts.fetch(part.artifactId, destination, this.scope.session_id);
      } catch (error) {
        // No artifact store means no attachment can exist to fetch; the turn goes on with the text.
        if (!this.artifacts.degradedBy(error)) throw error;
        break;
      }
      attachments.push({ id: part.artifactId, path: destination });
    }
    this.withdrawUnconfiguredTools();
    await this.registerPlatformTools();
    return { ...history, memories, attachments, userMessage: this.turn.message.content };
  }

  /** Names of the first-party platform tools this turn may call (after `prepare`). */
  platformTools(): string[] {
    return [...this.platformToolNames];
  }

  /** Source coverage gathered from the platform tools called so far; undefined when none was. */
  answerEvidence(): AnswerEvidence | undefined {
    return this.evidence.compose();
  }

  tools(): AgentTool[] {
    return [...this.registered.values()].map(tool => ({ ...tool, handler: input => this.executeTool(tool.name, input) }));
  }

  async executeTool(name: string, input: Record<string, unknown>): Promise<AgentToolResult> {
    const tool = this.registered.get(name);
    if (!tool) throw new ChatDataError('invalid_request');
    const parsed = z.object(tool.inputSchema).strict().safeParse(input);
    if (!parsed.success) throw new ChatDataError('invalid_request');
    try {
      return await tool.handler(parsed.data);
    } catch (error) {
      // A store the gateway reports unconfigured mid-turn is a tool error the model can read,
      // not a failed turn. The tool stays registered: the model already holds this turn's tool
      // list, and an unknown name would be refused as invalid_request.
      if (!this.memory.degradedBy(error) && !this.artifacts.degradedBy(error)) throw error;
      return { content: [{ type: 'text', text: `${name} is unavailable: this gateway has no store configured for it.` }], isError: true };
    }
  }

  async record(assistantContent: string, replyGranularity?: 'row'): Promise<void> {
    const messageId = await this.context.recordReply({
      sessionId: this.scope.session_id,
      userMessage: { role: 'user', content: this.turn.message.content },
      assistantMessage: { role: 'assistant', content: assistantContent },
    });
    const evidence = this.answerEvidence();
    await this.client.submitTurnResult({ outcome: 'completed', message_id: messageId,
      ...(replyGranularity ? { reply_granularity: replyGranularity } : {}),
      ...(evidence ? { evidence } : {}) });
  }
}
