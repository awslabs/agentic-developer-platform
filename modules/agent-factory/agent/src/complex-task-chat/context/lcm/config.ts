/**
 * LCM configuration — budgets, fresh tail size, chunk sizes.
 */
export interface LcmConfig {
  /** Number of most-recent raw messages protected from eviction */
  freshTailCount: number;
  /** Token threshold to trigger leaf compaction */
  leafChunkTokens: number;
  /** Target token count for a leaf summary */
  leafTargetTokens: number;
  /** Timeout for summarization calls */
  summaryTimeoutMs: number;
  /** Max compaction passes per record() call (1 for Phase 1) */
  maxTurnsPerCompaction: number;
  /** Model for summarization */
  summaryModel: string;
  /** Endpoint: 'bedrock' or 'gateway' */
  summaryEndpoint: 'bedrock' | 'gateway';
  /** Session TTL in seconds (default 90 days) */
  sessionTtlSeconds: number;
  // Phase 2+ knobs
  incrementalMaxDepth: number;
  leafMinFanout: number;
  condensedMinFanout: number;
  condensedTargetTokens: number;
}

export const DEFAULT_LCM_CONFIG: LcmConfig = {
  freshTailCount: 16,
  leafChunkTokens: 20_000,
  leafTargetTokens: 1200,
  summaryTimeoutMs: 60_000,
  maxTurnsPerCompaction: 1,
  summaryModel: 'global.anthropic.claude-sonnet-4-6',
  summaryEndpoint: 'bedrock',
  sessionTtlSeconds: 90 * 86400, // 90 days
  // Phase 2+ (disabled in Phase 1)
  incrementalMaxDepth: 0,
  leafMinFanout: 8,
  condensedMinFanout: 4,
  condensedTargetTokens: 2000,
};

export function loadLcmConfig(env: Record<string, string | undefined> = process.env): LcmConfig {
  return {
    ...DEFAULT_LCM_CONFIG,
    freshTailCount: parseInt(env.LCM_FRESH_TAIL_COUNT ?? '') || DEFAULT_LCM_CONFIG.freshTailCount,
    leafChunkTokens: parseInt(env.LCM_LEAF_CHUNK_TOKENS ?? '') || DEFAULT_LCM_CONFIG.leafChunkTokens,
    leafTargetTokens: parseInt(env.LCM_LEAF_TARGET_TOKENS ?? '') || DEFAULT_LCM_CONFIG.leafTargetTokens,
    summaryModel: env.LCM_SUMMARY_MODEL ?? DEFAULT_LCM_CONFIG.summaryModel,
    summaryEndpoint: (env.LCM_SUMMARY_ENDPOINT as 'bedrock' | 'gateway') ?? DEFAULT_LCM_CONFIG.summaryEndpoint,
    sessionTtlSeconds: parseInt(env.SESSION_TTL_SECONDS ?? '') || DEFAULT_LCM_CONFIG.sessionTtlSeconds,
  };
}
