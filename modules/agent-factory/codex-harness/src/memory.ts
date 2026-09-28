/** Host-side memory port. No backend SDK, credentials or provider selection in personas.
 * Integration with gateway authorization and run lifecycle is still required.
 */
import { z } from "zod";

const identifier = z.string().min(1).max(256);
const scopeSchema = z.object({
  tenantId: identifier,
  /** Gateway-selected private/shared namespace, never inferred from model text. */
  namespaceId: identifier,
  personaKey: identifier,
}).strict();
export type MemoryScope = z.infer<typeof scopeSchema>;
const recordSchema = z.object({
  id: identifier,
  scope: scopeSchema,
  summary: z.string().min(1).max(8192),
  sourceRunId: identifier,
  evidenceIds: z.array(identifier).min(1).max(32),
  confidence: z.enum(["proposed", "verified"]),
  expiresAt: z.number().int().positive().safe(),
}).strict();
export type MemoryRecord = z.infer<typeof recordSchema>;
export interface MemoryQuery {
  text: string;
  maxRecords: number;
  maxBytes: number;
}
export interface MemoryWriteReceipt {
  operationKey: string;
  outcome: "stored" | "disabled" | "unknown";
  recordId?: string;
}
export interface MemoryProvider {
  readonly id: string;
  readonly revision: string;
  /** Return relevance-ranked records, scoped and bounded by the host request. */
  retrieve(scope: Readonly<MemoryScope>, query: Readonly<MemoryQuery>, signal: AbortSignal): Promise<unknown>;
  /** Persist idempotently by scope + operationKey. Changed content must conflict.
   * Lost acknowledgements return unknown; the caller must not blindly replay.
   */
  save(scope: Readonly<MemoryScope>, record: Readonly<MemoryRecord>, operationKey: string,
    signal: AbortSignal): Promise<MemoryWriteReceipt>;
}
export class DisabledMemoryProvider implements MemoryProvider {
  readonly id = "none";
  readonly revision = "1";
  async retrieve(_scope: MemoryScope, _query: MemoryQuery, signal: AbortSignal) {
    signal.throwIfAborted();
    return [];
  }
  async save(_scope: MemoryScope, _record: MemoryRecord, operationKey: string, signal: AbortSignal): Promise<MemoryWriteReceipt> {
    signal.throwIfAborted();
    return { operationKey, outcome: "disabled" };
  }
}

/** Inject only a host-selected provider after current memory authority is checked.
 * This boundary validates data; it does not grant read/write or sharing authority.
 * Lifecycle integration must enforce timeout, token budget, redaction, and safe
 * write settlement independently. Retrieval content remains untrusted evidence.
 */
export class RunMemory {
  private readonly scope: Readonly<MemoryScope>;
  readonly provider: Readonly<{ id: string; revision: string }>;
  constructor(private readonly backend: MemoryProvider, scope: MemoryScope,
    private readonly clock: () => number = Date.now) {
    this.scope = Object.freeze(scopeSchema.parse(scope));
    this.provider = Object.freeze({ id: identifier.parse(backend.id), revision: identifier.parse(backend.revision) });
  }
  private scoped(record: MemoryRecord) {
    return record.scope.tenantId === this.scope.tenantId
      && record.scope.namespaceId === this.scope.namespaceId
      && record.scope.personaKey === this.scope.personaKey;
  }
  async retrieve(query: MemoryQuery, signal: AbortSignal): Promise<MemoryRecord[]> {
    const request = Object.freeze(z.object({ text: z.string().max(8192),
      maxRecords: z.number().int().min(1).max(32),
      maxBytes: z.number().int().min(1).max(32768),
    }).strict().parse(query));
    signal.throwIfAborted();
    const response = await this.backend.retrieve(this.scope, request, signal);
    signal.throwIfAborted();
    const records = z.array(recordSchema).max(request.maxRecords).parse(response);
    if (records.some(record => !this.scoped(record))) throw new Error("Memory scope mismatch");
    if (new Set(records.map(record => record.id)).size !== records.length) throw new Error("Duplicate memory identity");
    let bytes = 0;
    const selected: MemoryRecord[] = [];
    for (const record of records) {
      if (record.expiresAt <= this.clock()) continue;
      const size = Buffer.byteLength(JSON.stringify(record), "utf8");
      if (bytes + size > request.maxBytes) continue;
      bytes += size;
      selected.push(record);
    }
    return selected;
  }
  async save(record: MemoryRecord, operationKey: string, signal: AbortSignal): Promise<MemoryWriteReceipt> {
    const value = recordSchema.parse(record);
    const key = identifier.parse(operationKey);
    if (!this.scoped(value) || value.expiresAt <= this.clock()) throw new Error("Invalid memory write scope or expiry");
    signal.throwIfAborted();
    const result = await this.backend.save(this.scope, value, key, signal);
    // A cancellation after submission cannot prove whether persistence occurred.
    if (signal.aborted) return { operationKey: key, outcome: "unknown" };
    const receipt = z.object({ operationKey: identifier, outcome: z.enum(["stored", "disabled", "unknown"]),
      recordId: identifier.optional(),
    }).strict().parse(result);
    if (receipt.operationKey !== key || (receipt.outcome === "stored" && receipt.recordId !== value.id)) {
      throw new Error("Memory receipt identity mismatch; outcome unknown");
    }
    return receipt;
  }
}
