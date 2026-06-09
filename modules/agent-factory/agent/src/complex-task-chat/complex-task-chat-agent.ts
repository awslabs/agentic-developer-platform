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
import { getChannelDirective, getChannelEffort } from './channel-profiles';
import { loadPersona, composeSystemPrompt } from './persona-loader';
import { runQuery } from './run-query';
import { SqsClient, TaskPayload, AgUiEventEnvelope } from './sqs-client';
import { AgentTool } from './context/types';
import { ArtifactRef } from './artifacts/port';
import {
  AgUiEventType,
  agUiTimestamp,
  agUiId,
  type AgUiEvent,
} from './ag-ui-events';
import { Scrubber } from './context/scrubber';
import { vaultToolsForTurn } from './vault/tools';
import { VaultGatewayClient } from './vault/gateway-client';
import { createCredsInjector, CredsInjector } from '../aws-creds-injector';
import { buildPersonalContextIdentity, getPersonalContextEnvVars } from './personal-context-headers';
import { recallAtTaskStart, RECALL_ENABLED } from './recall-at-task-start';

const TOKEN_BUDGET = Number(process.env.CONTEXT_TOKEN_BUDGET ?? 150_000);

/**
 * Feature flag: enable vault credential tools. Requires ENABLE_USER_CREDENTIALS=1
 * AND a valid VAULT_GATEWAY_URL + VAULT_INTERNAL_API_KEY. When off, vault tools
 * are simply not registered.
 */
const VAULT_ENABLED = (process.env.ENABLE_USER_CREDENTIALS ?? '0') === '1';
const VAULT_GATEWAY_URL = process.env.VAULT_GATEWAY_URL ?? '';
const VAULT_INTERNAL_API_KEY = process.env.VAULT_INTERNAL_API_KEY ?? '';

/**
 * Feature flag: emit AG-UI events alongside legacy frames. Set
 * AGUI_EVENTS_ENABLED=1 to enable. During the backward-compat window
 * both shapes are emitted; after one week of stable operation the legacy
 * path will be removed.
 */
const AGUI_ENABLED = (process.env.AGUI_EVENTS_ENABLED ?? '1') === '1';

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
    // Stage A (#184): extended identity claims from JWT
    org_id,
    team_id,
    department_id,
    account_type,
    role: user_role,
    // Issue #1289: Cognito sub for personal-context identity propagation
    cognito_sub,
    // Delivery routing — echoed into every sendResponse() so the response
    // Lambda knows which channel/connection to deliver to. Missing any of
    // these caused WS replies to silently fall back to REST polling.
    thread_id,
    connection_id,
    channel,
    platform_data,
  } = task;

  // Stage A (#184): log full identity context at INFO for audit trail.
  console.log(
    `[chat-agent] Processing task ${task_id} for session ${session_id} ` +
    `(channel=${channel ?? 'none'}, conn=${connection_id ? 'set' : 'none'}, agui=${AGUI_ENABLED}) ` +
    `TokenContext: user_id=${user_id}, org_id=${org_id ?? 'none'}, team_id=${team_id ?? 'none'}, ` +
    `account_type=${account_type ?? 'none'}, role=${user_role ?? 'none'}, department_id=${department_id ?? 'none'}`,
  );

  /** Helper to emit an AG-UI event (best-effort, swallows errors). */
  const emitAgUi = async (event: AgUiEvent): Promise<void> => {
    if (!AGUI_ENABLED) return;
    try {
      const envelope: AgUiEventEnvelope = {
        task_id,
        session_id,
        status: 'ag_ui',
        ag_ui_event: true,
        event,
        thread_id,
        connection_id,
        channel,
        channel_metadata: platform_data,
      };
      await deps.sqs.sendAgUiEvent(envelope);
    } catch (err) {
      console.warn(`[chat-agent] AG-UI event emit failed (non-fatal): ${(err as Error).message}`);
    }
  };

  // AG-UI message ID for the assistant reply — stable across the whole turn
  const agUiMsgId = agUiId('msg');

  try {
    // AG-UI: RUN_STARTED
    await emitAgUi({
      event_type: AgUiEventType.RUN_STARTED,
      threadId: session_id,
      runId: task_id,
      timestamp: agUiTimestamp(),
    });

    // Placeholder "Thinking..." bubble — fires immediately when the worker
    // picks up the task, BEFORE the 1-3s of DDB/LCM/persona loading and the
    // 5-30s Bedrock turn. The ingest Lambda already emits an initial "On it"
    // bubble, but there's a 10-18s pod cold-start gap between that and the
    // real reply. This placeholder fills that gap with a visible bubble so
    // the user sees the agent is actively working instead of silent limbo.
    //
    // The placeholder uses its own messageId and closes cleanly
    // (START → CONTENT → END) so the frontend treats it as a completed
    // message and opens a fresh bubble for the real reply. Not persisted
    // to DDB — only the real reply is recorded via deps.context.record().
    const thinkingMsgId = agUiId('msg-thinking');
    await emitAgUi({
      event_type: AgUiEventType.TEXT_MESSAGE_START,
      messageId: thinkingMsgId,
      role: 'assistant',
      timestamp: agUiTimestamp(),
    });
    await emitAgUi({
      event_type: AgUiEventType.TEXT_MESSAGE_CONTENT,
      messageId: thinkingMsgId,
      delta: 'Thinking...',
      timestamp: agUiTimestamp(),
    });
    await emitAgUi({
      event_type: AgUiEventType.TEXT_MESSAGE_END,
      messageId: thinkingMsgId,
      timestamp: agUiTimestamp(),
    });

    // Defense-in-depth ownership check (creates header on first access; refuses
    // if existing owner differs). Stage A (#184): passes extended identity for
    // team-aware validation.
    await deps.context.assertOwnership(session_id, user_id, tenant_id, {
      orgId: org_id,
      teamId: team_id,
      departmentId: department_id,
      accountType: account_type,
    });

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

    const channelDirective = getChannelDirective(channel ?? '');

    // Stage C (#186): build <user-attachments> block when the message carries
    // attachment IDs. The agent can use `fetch_artifact` to read these files.
    const attachments = task.attachments ?? [];
    let attachmentBlock = '';
    if (attachments.length > 0) {
      const lines = attachments.map(id => `  <attachment id="${id}" />`);
      attachmentBlock =
        '\n\n<user-attachments>\n' +
        'The user attached files to this message. Use the fetch_artifact tool with the artifact ID to read each file.\n' +
        lines.join('\n') + '\n' +
        '</user-attachments>';
    }

    // Vault credential tools — gated by ENABLE_USER_CREDENTIALS + user_id presence.
    // Per-task scrubber: lives for one run, destroyed on task completion.
    const scrubber = new Scrubber();
    let vaultTools: AgentTool[] = [];
    let credentialsSummary = '';
    let credsInjector: CredsInjector | null = null;
    if (VAULT_ENABLED && user_id && VAULT_GATEWAY_URL && VAULT_INTERNAL_API_KEY) {
      const vaultClient = new VaultGatewayClient({
        baseUrl: VAULT_GATEWAY_URL,
        apiKey: VAULT_INTERNAL_API_KEY,
      });
      vaultTools = vaultToolsForTurn({
        userId: user_id,
        agentId: agent_type,
        taskId: task_id,
        scrubber,
        client: vaultClient,
      });

      // Issue #586: Create a per-task credentials injector to scope AWS env
      // vars for the agent's bash subshells. The agent's `aws ...` commands
      // will use the user's assumed role instead of pod IRSA.
      credsInjector = createCredsInjector({
        userId: user_id,
        agentId: agent_type,
        taskId: task_id,
        vaultClient,
      });

      // Fetch credential list for system-prompt injection (best-effort)
      try {
        const creds = await vaultClient.listCredentials(user_id);
        if (creds.length > 0) {
          const lines = creds.map(c =>
            `  - ${c.service} (${c.credential_type})${c.label ? ` — ${c.label}` : ''}`,
          );
          credentialsSummary =
            '\n\nAvailable credentials for this user:\n' +
            lines.join('\n') +
            '\n\nUse http_request_with_credential(service="<name>", ...) to make authenticated HTTP calls.\n' +
            'Use materialize_user_credential(service="<name>") to get a file path for SSH keys / certs / config files.\n' +
            'Use get_user_credential_raw(service="<name>", purpose="<reason>") only when neither of the above works.';
        }
      } catch (err) {
        console.warn(`[chat-agent] Failed to fetch credentials list (non-fatal): ${(err as Error).message}`);
      }
    }

    // Issue #586: system-prompt hint when no AWS credential is connected.
    // The agent needs the "why" in its prompt to give a helpful response when
    // `aws ...` returns "Unable to locate credentials".
    let awsEnvHint = '';
    if (credsInjector) {
      // Eagerly resolve creds so hasCredential() reflects reality before prompt assembly.
      await credsInjector.getScopedEnv();
      awsEnvHint = credsInjector.hasCredential()
        ? '\n\nUser has a connected AWS account. AWS commands (aws ..., boto3, etc.) will use their account automatically — just run them directly.'
        : '\n\nUser has NOT connected an AWS account. If they ask for AWS operations, tell them to visit /settings/credentials to connect one.';
    }

    // Issue #1293: Recall-at-task-start — retrieve the user's most relevant
    // prior learnings from personal-context and inject into system prompt.
    // Gated by PERSONAL_CONTEXT_RECALL_ENABLED (default off). Graceful
    // degradation: if recall fails, the task proceeds normally.
    const personalContextIdentity = buildPersonalContextIdentity({
      cognito_sub,
      tenant_id,
      user_id,
    });
    let priorExperienceSection = '';
    if (RECALL_ENABLED) {
      const recallResult = await recallAtTaskStart(
        personalContextIdentity,
        message,
        agent_type,
      );
      if (recallResult.warning) {
        console.warn(`[chat-agent] ${recallResult.warning}`);
      }
      priorExperienceSection = recallResult.promptSection;
    }

    const systemPrompt = composeSystemPrompt({
      base: channelDirective
        ? channelDirective + '\n\n' + persona.baseSystemPrompt + attachmentBlock + credentialsSummary + awsEnvHint
        : persona.baseSystemPrompt + attachmentBlock + credentialsSummary + awsEnvHint,
      personaLearnings: persona.learnings,
      memories: memBlock,
      priorExperience: priorExperienceSection,
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
      // Stage B (#185): pass identity so artifacts get team-scoped access control
      identity: org_id ? { orgId: org_id, teamId: team_id, userId: user_id } : undefined,
    });

    const tools: AgentTool[] = [
      ...deps.context.tools(),
      ...deps.memory.tools(),
      ...artifactTools,
      ...vaultTools,
    ];

    // Build per-tool input sanitizers for AG-UI event sanitization (#137).
    // Vault tools declare inputSummarySanitizer to strip credential-bearing fields.
    const toolSanitizers = new Map<string, (input: Record<string, unknown>) => Record<string, unknown>>();
    for (const t of vaultTools) {
      const sanitizable = t as { inputSummarySanitizer?: (input: Record<string, unknown>) => Record<string, unknown> };
      if (sanitizable.inputSummarySanitizer) {
        toolSanitizers.set(t.name, sanitizable.inputSummarySanitizer);
      }
    }

    // Issue #586: Get scoped env for the agent's bash subshells. This env has
    // pod-IRSA stripped and user's assumed-role creds injected. When no injector
    // is available (vault disabled), omit `env` so the SDK defaults to process.env.
    let scopedEnv = credsInjector ? await credsInjector.getScopedEnv() : undefined;

    // Issue #1289: Inject personal-context identity env vars into the subprocess
    // environment. These are read by the Context MCP Server client to set
    // X-Owner-Sub / X-Tenant-Id headers. Built from TRUSTED dispatch metadata
    // (SQS message fields set by the gateway/webhook-ingress), never from
    // agent/LLM input. The identity is frozen at construction time.
    // NOTE: personalContextIdentity is built earlier (Issue #1293 recall hook).
    const pcEnvVars = getPersonalContextEnvVars(personalContextIdentity);
    if (Object.keys(pcEnvVars).length > 0) {
      // Merge into scoped env if it exists, otherwise create a minimal env
      // with just the personal-context vars (the SDK will merge with process.env).
      scopedEnv = scopedEnv
        ? { ...scopedEnv, ...pcEnvVars }
        : { ...process.env, ...pcEnvVars } as Record<string, string | undefined>;
    }

    const result = await runQuery({
      systemPrompt,
      history: ctx.messages,
      userMessage: message,
      tools,
      toolSanitizers: toolSanitizers.size > 0 ? toolSanitizers : undefined,
      model: persona.modelOverride ?? process.env.ANTHROPIC_MODEL,
      cwd: '/tmp/workspace',
      env: scopedEnv,
      effort: getChannelEffort(channel ?? ''),
      // Forward mid-turn progress to the user over the same channel as the
      // final reply. Keeps the WebSocket warm (API Gateway's 10-min idle
      // timeout resets on any data frame) and gives the user something to
      // look at during long research turns.
      onProgress: async event => {
        const text =
          event.type === 'tool_use'
            ? renderToolUseProgress(event.tool_name, event.input_summary)
            : event.type === 'heartbeat'
              ? 'thinking...'
              : `💭 ${event.preview}`;

        // Legacy progress frame (backward compat)
        await deps.sqs.sendProgress({
          task_id,
          session_id,
          status: 'progress',
          kind: event.type,
          text,
          turn: event.turn,
          thread_id,
          connection_id,
          channel,
          channel_metadata: platform_data,
        });

        // AG-UI events
        if (event.type === 'tool_use') {
          const toolCallId = agUiId('tc');
          await emitAgUi({
            event_type: AgUiEventType.TOOL_CALL_START,
            toolCallId,
            toolCallName: event.tool_name,
            parentMessageId: agUiMsgId,
            timestamp: agUiTimestamp(),
          });
          if (event.input_summary) {
            await emitAgUi({
              event_type: AgUiEventType.TOOL_CALL_ARGS,
              toolCallId,
              delta: event.input_summary,
              timestamp: agUiTimestamp(),
            });
          }
          await emitAgUi({
            event_type: AgUiEventType.TOOL_CALL_END,
            toolCallId,
            timestamp: agUiTimestamp(),
          });
        } else if (event.type === 'heartbeat') {
          // Heartbeat → STATE_DELTA with a "heartbeat" patch
          await emitAgUi({
            event_type: AgUiEventType.STATE_DELTA,
            delta: [
              { op: 'replace', path: '/heartbeat', value: { turn: event.turn, ts: Date.now() } },
            ],
            timestamp: agUiTimestamp(),
          });
        } else if (event.type === 'thinking') {
          // Thinking preview → TEXT_MESSAGE_CONTENT on the assistant bubble
          await emitAgUi({
            event_type: AgUiEventType.TEXT_MESSAGE_CONTENT,
            messageId: agUiMsgId,
            delta: event.preview,
            timestamp: agUiTimestamp(),
          });
        }
      },
    });

    await deps.context.record({
      sessionId: session_id,
      userMessage: { role: 'user', content: scrubber.scrub(message) },
      assistantMessage: { role: 'assistant', content: scrubber.scrub(result.text) },
    });

    checkDeliveryConsistency(result.text, publishCount);

    // AG-UI: TEXT_MESSAGE_START → CONTENT → END → RUN_FINISHED
    await emitAgUi({
      event_type: AgUiEventType.TEXT_MESSAGE_START,
      messageId: agUiMsgId,
      role: 'assistant',
      timestamp: agUiTimestamp(),
    });
    await emitAgUi({
      event_type: AgUiEventType.TEXT_MESSAGE_CONTENT,
      messageId: agUiMsgId,
      delta: result.text,
      timestamp: agUiTimestamp(),
    });
    await emitAgUi({
      event_type: AgUiEventType.TEXT_MESSAGE_END,
      messageId: agUiMsgId,
      timestamp: agUiTimestamp(),
    });
    // State delta with tokens/cost metadata
    await emitAgUi({
      event_type: AgUiEventType.STATE_DELTA,
      delta: [
        { op: 'replace', path: '/tokens', value: result.tokens },
        { op: 'replace', path: '/turnCount', value: result.turnCount },
      ],
      timestamp: agUiTimestamp(),
    });
    await emitAgUi({
      event_type: AgUiEventType.RUN_FINISHED,
      threadId: session_id,
      runId: task_id,
      result: { tokens: result.tokens, turnCount: result.turnCount },
      timestamp: agUiTimestamp(),
    });

    // Legacy terminal response (thread bookkeeping happens here)
    await deps.sqs.sendResponse({
      task_id,
      session_id,
      text: result.text,
      tokens: result.tokens,
      status: 'completed',
      artifacts: publishedRefs,
      thread_id,
      connection_id,
      channel,
      channel_metadata: platform_data,
    });

    if (msg.ReceiptHandle) {
      await deps.sqs.deleteMessage(msg.ReceiptHandle);
    }

    console.log(`[chat-agent] Task ${task_id} completed successfully`);
  } catch (err) {
    console.error(`[chat-agent] Task ${task_id} failed:`, (err as Error).message);

    // AG-UI: RUN_ERROR
    await emitAgUi({
      event_type: AgUiEventType.RUN_ERROR,
      message: (err as Error).message,
      code: 'AGENT_ERROR',
      timestamp: agUiTimestamp(),
    });

    // Legacy terminal response (thread bookkeeping happens here)
    await deps.sqs.sendResponse({
      task_id,
      session_id,
      text: `error: ${(err as Error).message}`,
      status: 'failed',
      thread_id,
      connection_id,
      channel,
      channel_metadata: platform_data,
    });

    // Do not delete — DLQ policy applies
    throw err;
  }
}

/**
 * Render a user-facing progress line for a tool_use event. Keep these short
 * and specific — they're status updates, not full responses.
 */
function renderToolUseProgress(toolName: string, inputSummary: string): string {
  const preview = inputSummary ? ` — ${inputSummary}` : '';
  switch (toolName) {
    case 'WebSearch':
      return `🔍 Searching${preview ? `: ${inputSummary}` : '...'}`;
    case 'WebFetch':
      return `🌐 Fetching${preview ? `: ${inputSummary}` : ' page'}...`;
    case 'Bash':
      return `💻 Running${preview}`;
    case 'Read':
      return `📖 Reading${preview}`;
    case 'Write':
      return `✏️ Writing${preview}`;
    case 'Edit':
      return `✏️ Editing${preview}`;
    case 'Glob':
      return `📂 Searching files${preview ? `: ${inputSummary}` : ''}`;
    case 'Grep':
      return `🔎 Searching text${preview ? `: ${inputSummary}` : ''}`;
    case 'Skill':
      return `🎯 Using skill${preview}`;
    default:
      // MCP tools carry their mcp__chat-agent-tools__<name> prefix; trim it.
      const pretty = toolName.replace(/^mcp__chat-agent-tools__/, '');
      return `🛠️ ${pretty}${preview}`;
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
