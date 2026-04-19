/**
 * Complex Task Chat Agent — entrypoint
 *
 * SQS FIFO consumer that processes one message per pod invocation.
 * Composes: ContextManager + MemoryProvider + ArtifactStore + runQuery
 *
 * Per design doc section 5 — this is the only file that knows about all
 * three ports. Neither context nor memory imports the other.
 */
import { buildContextManager } from './context/factory';
import { buildMemoryProvider } from './memory/factory';
import { buildArtifactStore } from './artifacts/factory';
import { loadPersona, composeSystemPrompt } from './persona-loader';
import { runQuery } from './run-query';
import { SqsClient, TaskPayload } from './sqs-client';
import { AgentTool } from './context/types';
import { ArtifactRef } from './artifacts/port';

const TOKEN_BUDGET = Number(process.env.CONTEXT_TOKEN_BUDGET ?? 150_000);

function estimateTokens(text: string): number {
  return Math.ceil(text.length / 4);
}

async function main(): Promise<void> {
  console.log('[chat-agent] Starting complex-task-chat-agent');

  const context = buildContextManager();
  const memory = buildMemoryProvider();
  const artifacts = buildArtifactStore();
  const sqs = new SqsClient();

  // KEDA ScaledJob: process one message and exit
  const messages = await sqs.receive();
  if (messages.length === 0) {
    console.log('[chat-agent] No messages in queue, exiting');
    return;
  }

  for (const msg of messages) {
    await processOne(msg, { context, memory, artifacts, sqs });
  }
}

async function processOne(
  msg: { Body?: string; ReceiptHandle?: string },
  deps: {
    context: ReturnType<typeof buildContextManager>;
    memory: ReturnType<typeof buildMemoryProvider>;
    artifacts: ReturnType<typeof buildArtifactStore>;
    sqs: SqsClient;
  },
): Promise<void> {
  const task: TaskPayload = JSON.parse(msg.Body ?? '{}');
  const {
    task_id,
    session_id,
    message,
    agent_type = 'developer',
    user_id,
    tenant_id,
    component,
  } = task;

  console.log(`[chat-agent] Processing task ${task_id} for session ${session_id}`);

  try {
    // Defense-in-depth ownership check (creates header on first access; refuses
    // if existing owner differs).
    await deps.context.assertOwnership(session_id, user_id, tenant_id);

    const scope = {
      user: user_id,
      tenant: tenant_id,
      component,
      persona: agent_type,
    };

    const persona = await loadPersona(agent_type, {
      memory: deps.memory,
      query: message,
      tokenBudget: 500,
    });

    const memBlock = await deps.memory.retrieve({
      query: message,
      scope: { user: scope.user, component: scope.component },
      tokenBudget: 500,
      kinds: ['preference', 'fact'],
    });

    const systemPrompt = composeSystemPrompt({
      base: persona.baseSystemPrompt,
      personaLearnings: persona.learnings,
      memories: memBlock,
    });
    const systemTokens = estimateTokens(systemPrompt);

    const ctx = await deps.context.assemble({
      sessionId: session_id,
      userMessage: message,
      tokenBudget: TOKEN_BUDGET - systemTokens,
    });

    // Per-turn artifact tools closed over session/task scope with a publish counter.
    // Replaces the prior magic `_sessionId` injection that silently sent every
    // artifact to `unknown/unknown/<filename>`.
    let publishCount = 0;
    const publishedRefs: ArtifactRef[] = [];
    const artifactTools = deps.artifacts.toolsForTurn({
      sessionId: session_id,
      taskId: task_id,
      onPublish: ref => {
        publishCount++;
        publishedRefs.push(ref);
      },
    });

    const tools: AgentTool[] = [
      ...deps.context.tools(),
      ...deps.memory.tools(),
      ...artifactTools,
    ];

    const result = await runQuery({
      systemPrompt,
      history: ctx.messages,
      userMessage: message,
      tools,
      model: persona.modelOverride ?? process.env.ANTHROPIC_MODEL,
      cwd: '/tmp/workspace',
    });

    await deps.context.record({
      sessionId: session_id,
      userMessage: { role: 'user', content: message },
      assistantMessage: { role: 'assistant', content: result.text },
    });

    checkDeliveryConsistency(result.text, publishCount);

    await deps.sqs.sendResponse({
      task_id,
      session_id,
      text: result.text,
      tokens: result.tokens,
      status: 'completed',
      artifacts: publishedRefs,
    });

    if (msg.ReceiptHandle) {
      await deps.sqs.deleteMessage(msg.ReceiptHandle);
    }

    console.log(`[chat-agent] Task ${task_id} completed successfully`);
  } catch (err) {
    console.error(`[chat-agent] Task ${task_id} failed:`, (err as Error).message);

    await deps.sqs.sendResponse({
      task_id,
      session_id,
      text: `error: ${(err as Error).message}`,
      status: 'failed',
    });

    // Do not delete — DLQ policy applies
    throw err;
  }
}

/**
 * Warn when the assistant reply contains delivery language but publish_artifact
 * was never invoked this turn. Uses the per-turn publish counter (not substring
 * matching on `art_*`) — the agent rarely quotes IDs verbatim, so regex matching
 * produced false positives.
 */
function checkDeliveryConsistency(reply: string, publishCount: number): void {
  if (publishCount > 0) return;

  const deliveryPatterns = [
    "here's the file",
    'download',
    'attached',
    'updated version',
    'here is the',
    "i've created",
    "i've generated",
  ];
  const hasDeliveryLanguage = deliveryPatterns.some(p =>
    reply.toLowerCase().includes(p),
  );

  if (hasDeliveryLanguage) {
    console.warn(
      '[chat-agent] WARNING: reply contains delivery language but no publish_artifact was called this turn. ' +
        'The agent may have described creating a file without actually publishing it.',
    );
  }
}

main().catch(err => {
  console.error('[chat-agent] Fatal error:', err);
  process.exit(1);
});
