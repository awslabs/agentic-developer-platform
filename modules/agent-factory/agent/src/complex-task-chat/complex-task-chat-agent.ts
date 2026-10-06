/**
 * Complex Task Chat Agent — entrypoint
 *
 * Retired queue worker. Only the scoped store composer remains available
 * for future sandbox transport; the entrypoint refuses execution.
 */
import { buildContextManager } from './context/factory';
import { buildMemoryProvider } from './memory/factory';
import { buildArtifactStore } from './artifacts/factory';
import type { TaskPayload } from './sqs-client';
import { buildDraftStore } from './draft/factory';
import { ChatDataClient } from './gateway/chat-data-client';
import { activityTools } from './activity/tools';
import type { AgentTool } from './context/types';
import { BedrockSummarizer } from './context/summarize/bedrock-summarizer';
import { Summarizer } from './context/summarize/port';
import { loadLcmConfig } from './context/lcm/config';
import { readIdentityToken } from '../lib/runIdentity';

const WORKSPACE_ROOT = '/tmp/workspace';

/** Same identifier grammar the ChatDataClient binds sessions with. */
const SESSION_ID_PATTERN = /^[A-Za-z0-9_.:-]{1,128}$/;

export interface ChatStores {
  diagnostics?: ChatDataClient;
  context: ReturnType<typeof buildContextManager>;
  memory: ReturnType<typeof buildMemoryProvider>;
  artifacts: ReturnType<typeof buildArtifactStore>;
  /** Issue #4208: persistence for the intent-intake draft panel. */
  draftStore: ReturnType<typeof buildDraftStore>;
  activityTools?: AgentTool[];
}

export interface ChatStoreDeps {
  /** Overrides the projected-token reader (tests). Default: ADP_WORKLOAD_TOKEN_FILE via readIdentityToken. */
  workloadToken?: () => Promise<string>;
  /** Overrides the compaction summarizer (tests). Default: BedrockSummarizer over the run routing proxy. */
  summarizer?: Summarizer;
  workspaceRoot?: string;
}

/**
 * Compose the four per-turn stores for one message (#6932).
 *
 * ADP_CHAT_DATA_ENABLED !== 'true': refuse direct store construction.
 *
 * ADP_CHAT_DATA_ENABLED === 'true': ONE workload-bound ChatDataClient is built from
 * the projected token file and the gateway https origin, then handed to every
 * factory. Every precondition is checked here, before any store or model call, and
 * a failure throws instead of falling back to a broad-role direct store.
 *
 * Base URL: ADP_CHAT_DATA_URL, the gateway's https origin. The client itself
 * carries the workload token and the session capability, so it cannot ride the
 * SigV4 loopback proxy (which strips X-Adp-Workload-Token and only serves
 * loopback http); it uses the same https front door the gateway already serves.
 */
export async function buildChatStores(
  env: Record<string, string | undefined>,
  task: Pick<TaskPayload, 'session_id'>,
  deps: ChatStoreDeps = {},
): Promise<ChatStores> {
  if (env.ADP_CHAT_DATA_ENABLED !== 'true') {
    throw new Error('Credentialed chat worker cannot build direct owner stores');
  }

  const direct = (['CONTEXT_STRATEGY', 'MEMORY_STRATEGY', 'ARTIFACT_STRATEGY'] as const).filter(key => env[key] !== 'gateway');
  if (direct.length > 0) {
    throw new Error(`Scoped chat data requires ${direct.map(key => `${key}=gateway`).join(', ')}; refusing direct-store fallback`);
  }
  if (!env.ADP_CHAT_DATA_URL) {
    throw new Error('Scoped chat data requires ADP_CHAT_DATA_URL (https origin of the gateway)');
  }
  const sessionId = task.session_id;
  if (typeof sessionId !== 'string' || !SESSION_ID_PATTERN.test(sessionId)) {
    throw new Error('Scoped chat data requires a valid task session_id');
  }
  let workloadToken = deps.workloadToken;
  if (!workloadToken) {
    const tokenFile = env.ADP_WORKLOAD_TOKEN_FILE;
    if (!tokenFile) throw new Error('Scoped chat data requires ADP_WORKLOAD_TOKEN_FILE (projected workload token)');
    // Re-read per exchange so a rotated projection is picked up on renewal.
    workloadToken = async () => readIdentityToken(tokenFile);
  }
  // Probe the token source once up front: an absent or unreadable projection must
  // stop the turn here, not surface as a mid-turn store failure.
  try {
    await workloadToken();
  } catch {
    throw new Error('Scoped chat data requires a readable workload token; refusing to start without one');
  }

  let client: ChatDataClient;
  try {
    client = new ChatDataClient({ baseUrl: env.ADP_CHAT_DATA_URL, workloadToken });
  } catch {
    throw new Error('Scoped chat data requires ADP_CHAT_DATA_URL to be an https origin without path, query or credentials');
  }
  const summarizer = deps.summarizer ?? new BedrockSummarizer(loadLcmConfig(env).summaryModel, env.AWS_REGION ?? 'us-east-1');
  const workspaceRoot = deps.workspaceRoot ?? WORKSPACE_ROOT;

  const context = buildContextManager(env, { client, summarizer });
  const memory = buildMemoryProvider(env, { client });
  const artifacts = buildArtifactStore(env, { client, sessionId, workspaceRoot });
  const draftStore = buildDraftStore(env, { client, sessionId });
  return { context, memory, artifacts, draftStore, diagnostics: client, activityTools: activityTools(client) };
}

export async function main(): Promise<void> {
  throw new Error('Credentialed chat worker retired; restricted sandbox supervisor required');
}

// startup.sh invokes this entrypoint; importing the store builder does not start a consumer.
if (require.main === module) {
  main().catch(err => {
    console.error('[chat-agent] Fatal error:', err);
    process.exit(1);
  });
}
