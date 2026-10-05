/**
 * Host-child process protocol, agent side — Task API T5 (#5798).
 *
 * The host (the Python worker) and this child exchange one JSON object per
 * newline over stdin/stdout, incrementally. Stdout is protocol-only; diagnostics
 * go to bounded sanitized stderr. Accepted design revision
 * b5761a4a2502aceaa9133afef552b567a19cb46e, design section 8; the normative shapes
 * are `docs/task-api/contracts/v1/schemas/process-protocol.schema.json`.
 *
 * ## Why this file validates rather than trusts
 *
 * The contract's forbidden-field rules are the containment boundary this whole
 * persona rests on, and a boundary that is only documented is not a boundary. A
 * `start` frame carrying `github_token`, or a `model.request` naming a `model`
 * and `endpoint`, is not a frame this agent should process defensively — it is a
 * protocol violation, because accepting it would mean agent-influenced content
 * had acquired a credential or chosen where inference goes. So both directions
 * are checked: inbound frames are refused, and outbound frames are refused
 * *before* they are written, so a coding mistake here cannot put a forbidden
 * field on the wire and have the host be the only thing that notices.
 *
 * ## Why there is no schema library
 *
 * This package has zero runtime dependencies on purpose (design section 3:
 * "independent package ... no agent SDK dependency", isolated dependency tree).
 * A JSON Schema validator would be a runtime dependency installed into the
 * shared worker image for one process's benefit. The checks below are written
 * against the same fixture corpus the schemas are, and
 * `protocol.test.ts` drives them with T0's published fixtures unchanged — so the
 * binding to the contract is by shared test data rather than by a shared library.
 */

/** Protocol version this build speaks. A mismatch fails the run. */
export const PROTOCOL_VERSION = 1;

/** Frame byte ceiling from the fixed pilot limits (limits.json, process_and_reporting). */
export const MAX_FRAME_BYTES = 65536;

/** Progress event byte ceiling from the same limits block. */
export const MAX_PROGRESS_EVENT_BYTES = 8192;

/**
 * Capabilities this agent actually implements.
 *
 * The contract permits only `input` and `cancel`, and requires that a child
 * advertise only what it implements: pause and resume are not implemented here,
 * and claiming them would let a caller be told yes for a verb with nothing behind
 * it. Ordered so the emitted `ready` frame is byte-stable.
 */
export const IMPLEMENTED_CAPABILITIES = ['input', 'cancel'] as const;

export type Capability = (typeof IMPLEMENTED_CAPABILITIES)[number];

/** The four authored stages of an investigation (design section 3). */
export type Stage = 'evidence_inventory' | 'analysis' | 'clarification' | 'synthesis';

/** Model operation outcome as reported by the host. */
export type OperationStatus = 'pending' | 'confirmed' | 'unknown' | 'rejected';

/** Failure codes this child may emit on an `error` frame. */
export type ChildErrorCode =
  | 'invalid_agent_output'
  | 'protocol_violation'
  | 'process_failed'
  | 'deadline_exceeded'
  | 'model_outcome_unknown';

/** Host-supplied model error codes. */
export type ModelErrorCode =
  | 'model_access_denied'
  | 'budget_exceeded'
  | 'authority_revoked'
  | 'model_outcome_unknown'
  | 'deadline_exceeded';

export interface InputArtifact {
  artifact_id: string;
  content_type: 'text/plain' | 'application/json';
  content: string;
}

export interface StartLimits {
  deadline_at?: string;
  max_turns?: number;
  max_output_tokens_per_turn?: number;
}

export interface StartFrame {
  protocol_version: 1;
  type: 'start';
  request_id: string;
  task_id: string;
  invocation_id: string;
  generation: number;
  runtime_attempt_id?: string;
  tool_grants?: string[];
  instructions: string;
  inputs?: Record<string, unknown>;
  acceptance_criteria?: string[];
  artifacts?: InputArtifact[];
  limits?: StartLimits;
}

export interface ArtifactReference {
  artifact_id: string;
  content_type: 'text/plain' | 'application/json';
  content_sha256: string;
  byte_length: number;
}

export interface WireStartFrame extends Omit<StartFrame, 'artifacts'> {
  artifacts?: Array<InputArtifact | ArtifactReference>;
}

export interface ArtifactChunkFrame {
  protocol_version: 1;
  type: 'artifact.chunk';
  request_id: string;
  task_id: string;
  artifact_id: string;
  content_type: 'text/plain' | 'application/json';
  content_sha256: string;
  sequence: number;
  total_bytes: number;
  data_base64: string;
  last: boolean;
}

export interface TurnMessage {
  command_id: string;
  text: string;
  reply_to?: string;
}

export interface TurnFrame {
  protocol_version: 1;
  type: 'turn';
  request_id: string;
  task_id: string;
  turn_id: string;
  turn_number: number;
  messages: TurnMessage[];
}

export interface ModelResultFrame {
  protocol_version: 1;
  type: 'model.result';
  request_id: string;
  task_id: string;
  turn_id: string;
  operation_status: OperationStatus;
  content?: unknown[] | null;
  stop_reason?: string | null;
  error_code?: ModelErrorCode | null;
}

export interface ReportAckFrame {
  protocol_version: 1;
  type: 'report.ack';
  request_id: string;
  task_id: string;
  report_id: string;
  sequence: number;
}

export interface CancelFrame {
  protocol_version: 1;
  type: 'cancel';
  request_id: string;
  task_id: string;
  command_id: string;
  intentional: true;
  reason?: string;
}

export type HostFrame = ArtifactChunkFrame | WireStartFrame | TurnFrame | ModelResultFrame | ReportAckFrame | CancelFrame;

export interface ReadyFrame {
  protocol_version: 1;
  type: 'ready';
  request_id: string;
  task_id: string;
  capabilities: Capability[];
}

export interface ProgressFrame {
  protocol_version: 1;
  type: 'progress';
  request_id: string;
  task_id: string;
  report_id: string;
  message: string;
  stage?: Stage;
  producer_timestamp?: string;
}

export interface ModelRequestFrame {
  protocol_version: 1;
  type: 'model.request';
  request_id: string;
  task_id: string;
  turn_id: string;
  messages: unknown[];
  max_tokens?: number;
  system?: string;
}

export interface InputRequiredFrame {
  protocol_version: 1;
  type: 'input.required';
  request_id: string;
  task_id: string;
  input_request_id: string;
  prompt: string;
}

export interface ResultFrame {
  protocol_version: 1;
  type: 'result';
  request_id: string;
  task_id: string;
  report: InvestigatorReport;
}

export interface CancelledFrame {
  protocol_version: 1;
  type: 'cancelled';
  request_id: string;
  task_id: string;
  command_id: string;
  partial_findings?: number | null;
}

export interface ErrorFrame {
  protocol_version: 1;
  type: 'error';
  request_id: string;
  task_id: string;
  code: ChildErrorCode;
  message: string;
}

export type ChildFrame =
  | ReadyFrame
  | ProgressFrame
  | ModelRequestFrame
  | InputRequiredFrame
  | ResultFrame
  | CancelledFrame
  | ErrorFrame;

/** Evidence provenance. Only caller-supplied sources exist; there is no fetch. */
export type EvidenceSource = 'inputs' | 'artifact' | 'instructions' | 'follow_up_input';

export interface EvidenceRef {
  ref: string;
  source: EvidenceSource;
  artifact_id?: string;
}

export interface Finding {
  statement: string;
  evidence_refs: string[];
  confidence?: 'low' | 'medium' | 'high';
}

export interface InvestigatorReport {
  documents?: Array<{ name: string; media_type: 'application/json'; content: string }>;
  summary: string;
  findings: Finding[];
  uncertainties: string[];
  recommendations: string[];
  evidence_refs: EvidenceRef[];
}

/**
 * A refused frame.
 *
 * Carries the contract reason rather than a generic parse message because the
 * host persists this and an operator reads it: "forbidden field github_token on
 * a start frame" is actionable, "invalid frame" is not. Bounded to the contract's
 * safe-message length since it travels to durable storage.
 */
export class ProtocolViolation extends Error {
  readonly isProtocolViolation = true as const;

  constructor(reason: string) {
    super(reason.length > 1000 ? `${reason.slice(0, 999)}…` : reason);
    this.name = 'ProtocolViolation';
  }
}

const UUID4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const TASK_ID = /^tsk_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const ARTIFACT_ID = /^art_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const RFC3339_UTC = /^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?Z$/;

/**
 * Fields no frame may carry in either direction.
 *
 * Split by the concern each group protects so a violation names which boundary
 * was crossed. `credential` is the containment boundary (the child holds none, so
 * no frame carries one); `binding` is the model-authority boundary (the host
 * chose the model, region and endpoint at admission); `hostLedger` is the
 * accounting boundary (the child reports content, the host owns spend and turn
 * counts, so a child-stated cost would be agent output writing a billing record);
 * `reasoning` keeps private deliberation and fabricated percentages off a public
 * progress surface.
 */
const FORBIDDEN = {
  credential: [
    'aws_access_key_id',
    'aws_secret_access_key',
    'aws_session_token',
    'github_token',
    'gateway_token',
    'run_credential',
    'api_key',
  ],
  binding: ['model', 'model_id', 'endpoint', 'api_key', 'region'],
  hostLedger: ['total_usd', 'turns_used'],
  reasoning: ['percent_complete', 'reasoning', 'thinking'],
} as const;

/**
 * Every field each frame type may carry.
 *
 * The contract sets `additionalProperties: false` on all twelve frames, so an
 * unrecognised field is a violation rather than something to ignore. Tolerating
 * unknown fields would be the more forgiving choice and the wrong one: a field
 * this build does not understand means the peer is speaking a protocol this build
 * does not implement, and proceeding would mean acting on a frame whose meaning
 * is partly unread. The explicit lists also mean a forbidden field is caught even
 * if someone adds a new one to the schema and not to {@link FORBIDDEN}.
 */
const FRAME_KEYS: Record<string, readonly string[]> = {
  'artifact.chunk': ['protocol_version', 'type', 'request_id', 'task_id', 'artifact_id', 'content_type', 'content_sha256', 'sequence', 'total_bytes', 'data_base64', 'last'],
  start: [
    'protocol_version',
    'type',
    'request_id',
    'task_id',
    'invocation_id',
    'generation',
    'runtime_attempt_id',
    'tool_grants',
    'instructions',
    'inputs',
    'acceptance_criteria',
    'artifacts',
    'limits',
  ],
  turn: ['protocol_version', 'type', 'request_id', 'task_id', 'turn_id', 'turn_number', 'messages'],
  'model.result': [
    'protocol_version',
    'type',
    'request_id',
    'task_id',
    'turn_id',
    'operation_status',
    'content',
    'stop_reason',
    'error_code',
  ],
  'report.ack': ['protocol_version', 'type', 'request_id', 'task_id', 'report_id', 'sequence'],
  cancel: ['protocol_version', 'type', 'request_id', 'task_id', 'command_id', 'intentional', 'reason'],
  ready: ['protocol_version', 'type', 'request_id', 'task_id', 'capabilities'],
  progress: [
    'protocol_version',
    'type',
    'request_id',
    'task_id',
    'report_id',
    'message',
    'stage',
    'producer_timestamp',
  ],
  'model.request': [
    'protocol_version',
    'type',
    'request_id',
    'task_id',
    'turn_id',
    'messages',
    'max_tokens',
    'system',
  ],
  'input.required': [
    'protocol_version',
    'type',
    'request_id',
    'task_id',
    'input_request_id',
    'prompt',
  ],
  result: ['protocol_version', 'type', 'request_id', 'task_id', 'report'],
  cancelled: ['protocol_version', 'type', 'request_id', 'task_id', 'command_id', 'partial_findings'],
  error: ['protocol_version', 'type', 'request_id', 'task_id', 'code', 'message'],
};

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function refuseUnknownFields(frame: Record<string, unknown>, type: string): void {
  const allowed = FRAME_KEYS[type];
  if (allowed === undefined) {
    throw new ProtocolViolation(`unknown frame type ${type}`);
  }
  for (const key of Object.keys(frame)) {
    if (!allowed.includes(key)) {
      throw new ProtocolViolation(`unrecognised field ${key} on a ${type} frame`);
    }
  }
}

function requireOnlyFields(
  value: Record<string, unknown>,
  fields: readonly string[],
  what: string,
): void {
  for (const key of Object.keys(value)) {
    if (!fields.includes(key)) {
      throw new ProtocolViolation(`unrecognised field ${key} on a ${what}`);
    }
  }
}

function refuseForbidden(
  frame: Record<string, unknown>,
  fields: readonly string[],
  what: string,
): void {
  for (const field of fields) {
    if (Object.hasOwn(frame, field)) {
      throw new ProtocolViolation(`forbidden field ${field} on a ${what}`);
    }
  }
}

function requireString(
  frame: Record<string, unknown>,
  field: string,
  what: string,
  opts: { pattern?: RegExp; min?: number; max?: number } = {},
): string {
  const value = frame[field];
  if (typeof value !== 'string') {
    throw new ProtocolViolation(`${what} requires a string ${field}`);
  }
  if (value.length < (opts.min ?? 1)) {
    throw new ProtocolViolation(`${what} field ${field} is shorter than its minimum`);
  }
  if (opts.max !== undefined && value.length > opts.max) {
    throw new ProtocolViolation(`${what} field ${field} exceeds its ${opts.max}-character bound`);
  }
  if (opts.pattern && !opts.pattern.test(value)) {
    throw new ProtocolViolation(`${what} field ${field} does not match its contract pattern`);
  }
  return value;
}

function requireInteger(
  frame: Record<string, unknown>,
  field: string,
  what: string,
  min: number,
  max?: number,
): number {
  const value = frame[field];
  if (typeof value !== 'number' || !Number.isInteger(value)) {
    throw new ProtocolViolation(`${what} requires an integer ${field}`);
  }
  if (value < min || (max !== undefined && value > max)) {
    throw new ProtocolViolation(`${what} field ${field} is outside its permitted range`);
  }
  return value;
}

/**
 * The checks every frame gets, in the order that produces the most useful refusal.
 *
 * Forbidden groups run first even though the unknown-field check would also catch
 * them, because the messages are not equally useful to whoever reads the failure:
 * "forbidden field github_token on a start frame" names a boundary that was
 * crossed, while "unrecognised field github_token" reads like a version skew. The
 * unknown-field check then catches everything the explicit lists do not, so a
 * field added to the schema without being added here still fails closed.
 */
function requireEnvelope(
  frame: Record<string, unknown>,
  type: string,
  forbidden: readonly (readonly string[])[] = [FORBIDDEN.credential],
): void {
  for (const group of forbidden) {
    refuseForbidden(frame, group, `${type} frame`);
  }
  refuseUnknownFields(frame, type);
  if (frame['protocol_version'] !== PROTOCOL_VERSION) {
    throw new ProtocolViolation(
      `unsupported protocol_version on a ${type} frame; this build speaks ${PROTOCOL_VERSION}`,
    );
  }
  requireString(frame, 'request_id', `${type} frame`, { pattern: UUID4 });
  requireString(frame, 'task_id', `${type} frame`, { pattern: TASK_ID });
}

/**
 * Parse and validate one host-to-child frame.
 *
 * Throws {@link ProtocolViolation} rather than returning a result union: every
 * caller's correct response to an invalid frame is identical — fail the run — and
 * a returned error object invites a caller to carry on with a partially trusted
 * frame. The contract is explicit that invalid protocol fails the run.
 */
export function parseHostFrame(line: string, maxTurns: 8 | 32 | 1000 = 1000): HostFrame {
  if (maxTurns !== 8 && maxTurns !== 32 && maxTurns !== 1000) throw new ProtocolViolation("Unsupported Task turn profile");
  const bytes = Buffer.byteLength(line, 'utf8');
  if (bytes > MAX_FRAME_BYTES) {
    throw new ProtocolViolation(`host frame of ${bytes} bytes exceeds the ${MAX_FRAME_BYTES}-byte bound`);
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(line);
  } catch {
    throw new ProtocolViolation('host frame is not valid JSON');
  }
  if (!isPlainObject(parsed)) {
    throw new ProtocolViolation('host frame is not a JSON object');
  }

  const type = parsed['type'];
  if (typeof type !== 'string') {
    throw new ProtocolViolation('host frame has no string type');
  }

  switch (type) {
    case 'artifact.chunk':
      return parseArtifactChunkFrame(parsed);
    case 'start':
      return parseStartFrame(parsed, maxTurns);
    case 'turn':
      return parseTurnFrame(parsed, maxTurns);
    case 'model.result':
      return parseModelResultFrame(parsed);
    case 'report.ack':
      return parseReportAckFrame(parsed);
    case 'cancel':
      return parseCancelFrame(parsed);
    default:
      throw new ProtocolViolation(`unknown host frame type ${type}`);
  }
}

function parseStartFrame(frame: Record<string, unknown>, maxTurns: number): WireStartFrame {
  // requireEnvelope refuses credentials before anything else is read: if a
  // credential is present, nothing about this frame should be processed,
  // including its instructions.
  requireEnvelope(frame, 'start');

  const instructions = requireString(frame, 'instructions', 'start frame', { max: 16000 });
  const generation = requireInteger(frame, 'generation', 'start frame', 1);
  requireString(frame, 'invocation_id', 'start frame', { pattern: UUID4 });

  const result: WireStartFrame = {
    protocol_version: PROTOCOL_VERSION,
    type: 'start',
    request_id: frame['request_id'] as string,
    task_id: frame['task_id'] as string,
    invocation_id: frame['invocation_id'] as string,
    generation,
    instructions,
  };

  if (frame['runtime_attempt_id'] !== undefined && frame['runtime_attempt_id'] !== null) {
    result.runtime_attempt_id = requireString(frame, 'runtime_attempt_id', 'start frame', {
      pattern: UUID4,
    });
  }

  if (frame['tool_grants'] !== undefined) {
    const grants = frame['tool_grants'];
    if (!Array.isArray(grants) || grants.length > 128 ||
        grants.some(value => typeof value !== 'string' || !/^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$/.test(value)) ||
        new Set(grants).size !== grants.length) {
      throw new ProtocolViolation('start frame tool_grants must be distinct bounded tool permissions');
    }
    result.tool_grants = [...grants];
  }

  if (frame['inputs'] !== undefined) {
    if (!isPlainObject(frame['inputs'])) {
      throw new ProtocolViolation('start frame inputs must be a JSON object');
    }
    result.inputs = frame['inputs'];
  }

  if (frame['acceptance_criteria'] !== undefined) {
    const criteria = frame['acceptance_criteria'];
    if (!Array.isArray(criteria) || criteria.length > 10) {
      throw new ProtocolViolation('start frame acceptance_criteria must be an array of at most 10 items');
    }
    for (const entry of criteria) {
      if (typeof entry !== 'string' || entry.length > 1000) {
        throw new ProtocolViolation('each acceptance criterion must be a string of at most 1000 characters');
      }
    }
    result.acceptance_criteria = criteria as string[];
  }

  if (frame['artifacts'] !== undefined) {
    const artifacts = frame['artifacts'];
    if (!Array.isArray(artifacts) || artifacts.length > 4) {
      throw new ProtocolViolation('start frame artifacts must be an array of at most 4 items');
    }
    let totalArtifactBytes = 0;
    result.artifacts = artifacts.map((entry) => {
      if (!isPlainObject(entry)) {
        throw new ProtocolViolation('each start frame artifact must be an object');
      }
      const embedded = Object.hasOwn(entry, 'content');
      requireOnlyFields(entry, embedded ? ['artifact_id', 'content_type', 'content'] : ['artifact_id', 'content_type', 'content_sha256', 'byte_length'], 'start frame artifact');
      const artifactId = requireString(entry, 'artifact_id', 'start frame artifact', {
        pattern: ARTIFACT_ID,
      });
      const contentType = entry['content_type'];
      if (contentType !== 'text/plain' && contentType !== 'application/json') {
        throw new ProtocolViolation(
          'start frame artifact content_type must be text/plain or application/json',
        );
      }
      if (!embedded) {
        const byteLength = requireInteger(entry, 'byte_length', 'artifact reference', 1, 262144);
        const digest = requireString(entry, 'content_sha256', 'artifact reference', { pattern: /^[0-9a-f]{64}$/ });
        totalArtifactBytes += byteLength;
        return { artifact_id: artifactId, content_type: contentType, content_sha256: digest, byte_length: byteLength };
      }
      const content = requireString(entry, 'content', 'start frame artifact', { min: 0 });
      const contentBytes = Buffer.byteLength(content, 'utf8');
      if (contentBytes > 262144) {
        throw new ProtocolViolation('start frame artifact content exceeds its 262144-byte bound');
      }
      totalArtifactBytes += contentBytes;
      return { artifact_id: artifactId, content_type: contentType, content };
    });
    if (totalArtifactBytes > 1048576) {
      throw new ProtocolViolation('start frame artifacts exceed their 1048576-byte total bound');
    }
  }

  if (frame['limits'] !== undefined) {
    const limits = frame['limits'];
    if (!isPlainObject(limits)) {
      throw new ProtocolViolation('start frame limits must be an object');
    }
    requireOnlyFields(
      limits,
      ['max_turns', 'max_output_tokens_per_turn', 'deadline_at'],
      'start frame limits',
    );
    const parsedLimits: StartLimits = {};
    if (limits['deadline_at'] !== undefined) {
      parsedLimits.deadline_at = requireString(limits, 'deadline_at', 'start frame limits', { pattern: RFC3339_UTC });
      if (!Number.isFinite(Date.parse(parsedLimits.deadline_at))) {
        throw new ProtocolViolation('start frame deadline must be a valid timestamp');
      }
    }
    if (limits['max_turns'] !== undefined) {
      parsedLimits.max_turns = requireInteger(limits, 'max_turns', 'start frame limits', 1, maxTurns);
    }
    if (limits['max_output_tokens_per_turn'] !== undefined) {
      parsedLimits.max_output_tokens_per_turn = requireInteger(
        limits,
        'max_output_tokens_per_turn',
        'start frame limits',
        1,
        10000,
      );
    }
    result.limits = parsedLimits;
  }

  return result;
}

function parseArtifactChunkFrame(frame: Record<string, unknown>): ArtifactChunkFrame {
  requireEnvelope(frame, 'artifact.chunk');
  const contentType = frame['content_type'];
  if (contentType !== 'text/plain' && contentType !== 'application/json') {
    throw new ProtocolViolation('artifact chunk has invalid content_type');
  }
  if (typeof frame['last'] !== 'boolean') throw new ProtocolViolation('artifact chunk last must be boolean');
  return {
    protocol_version: 1, type: 'artifact.chunk',
    request_id: frame['request_id'] as string, task_id: frame['task_id'] as string,
    artifact_id: requireString(frame, 'artifact_id', 'artifact chunk', { pattern: ARTIFACT_ID }),
    content_type: contentType,
    content_sha256: requireString(frame, 'content_sha256', 'artifact chunk', { pattern: /^[0-9a-f]{64}$/ }),
    sequence: requireInteger(frame, 'sequence', 'artifact chunk', 1, 8),
    total_bytes: requireInteger(frame, 'total_bytes', 'artifact chunk', 1, 262144),
    data_base64: requireString(frame, 'data_base64', 'artifact chunk', { min: 4, max: 43692, pattern: /^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/ }),
    last: frame['last'],
  };
}

function parseTurnFrame(frame: Record<string, unknown>, maxTurns: number): TurnFrame {
  requireEnvelope(frame, 'turn');

  const turnId = requireString(frame, 'turn_id', 'turn frame', { pattern: UUID4 });
  const turnNumber = requireInteger(frame, 'turn_number', 'turn frame', 1, maxTurns);
  const messages = frame['messages'];
  if (!Array.isArray(messages) || messages.length < 1) {
    throw new ProtocolViolation('turn frame requires at least one message');
  }

  const commandIds = new Set<string>();
  const parsedMessages = messages.map((entry) => {
    if (!isPlainObject(entry)) {
      throw new ProtocolViolation('each turn message must be an object');
    }
    requireOnlyFields(entry, ['command_id', 'text', 'reply_to'], 'turn message');
    const commandId = requireString(entry, 'command_id', 'turn message', { pattern: UUID4 });
    if (commandIds.has(commandId)) {
      throw new ProtocolViolation('a turn frame cannot list the same command_id more than once');
    }
    commandIds.add(commandId);
    const message: TurnMessage = {
      command_id: commandId,
      text: requireString(entry, 'text', 'turn message', { max: 4000 }),
    };
    if (entry['reply_to'] !== undefined && entry['reply_to'] !== null) {
      message.reply_to = requireString(entry, 'reply_to', 'turn message', { pattern: UUID4 });
    }
    return message;
  });

  return {
    protocol_version: PROTOCOL_VERSION,
    type: 'turn',
    request_id: frame['request_id'] as string,
    task_id: frame['task_id'] as string,
    turn_id: turnId,
    turn_number: turnNumber,
    messages: parsedMessages,
  };
}

function parseModelResultFrame(frame: Record<string, unknown>): ModelResultFrame {
  requireEnvelope(frame, 'model.result');

  const status = frame['operation_status'];
  if (
    status !== 'pending' &&
    status !== 'confirmed' &&
    status !== 'unknown' &&
    status !== 'rejected'
  ) {
    throw new ProtocolViolation('model.result frame requires a known operation_status');
  }

  const content = frame['content'];
  if (content !== undefined && content !== null && !Array.isArray(content)) {
    throw new ProtocolViolation('model.result content must be an array or null');
  }

  // The contract states these as schema conditionals; they are the two rules that
  // stop an unusable outcome from being mistaken for an answer, so they are
  // enforced here rather than left to the caller's discipline.
  if (status === 'unknown' && content !== null && content !== undefined) {
    throw new ProtocolViolation('an unknown model outcome must carry null content');
  }
  if (status === 'confirmed' && !Array.isArray(content)) {
    throw new ProtocolViolation('a confirmed model outcome must carry content blocks');
  }

  const result: ModelResultFrame = {
    protocol_version: PROTOCOL_VERSION,
    type: 'model.result',
    request_id: frame['request_id'] as string,
    task_id: frame['task_id'] as string,
    turn_id: requireString(frame, 'turn_id', 'model.result frame', { pattern: UUID4 }),
    operation_status: status,
  };
  if (content !== undefined) {
    result.content = content as unknown[] | null;
  }
  if (frame['stop_reason'] !== undefined) {
    const stopReason = frame['stop_reason'];
    if (stopReason !== null) {
      if (typeof stopReason !== 'string') {
        throw new ProtocolViolation('model.result stop_reason must be a string or null');
      }
      if (stopReason.length > 64) {
        throw new ProtocolViolation('model.result stop_reason exceeds its 64-character bound');
      }
    }
    result.stop_reason = stopReason;
  }
  if (frame['error_code'] !== undefined) {
    const code = frame['error_code'];
    const permitted: readonly (string | null)[] = [
      'model_access_denied',
      'budget_exceeded',
      'authority_revoked',
      'model_outcome_unknown',
      'deadline_exceeded',
      null,
    ];
    if (!permitted.includes(code as string | null)) {
      throw new ProtocolViolation('model.result error_code is not a contract failure code');
    }
    result.error_code = code as ModelErrorCode | null;
  }
  return result;
}

function parseReportAckFrame(frame: Record<string, unknown>): ReportAckFrame {
  requireEnvelope(frame, 'report.ack');
  return {
    protocol_version: PROTOCOL_VERSION,
    type: 'report.ack',
    request_id: frame['request_id'] as string,
    task_id: frame['task_id'] as string,
    report_id: requireString(frame, 'report_id', 'report.ack frame', { pattern: UUID4 }),
    sequence: requireInteger(frame, 'sequence', 'report.ack frame', 1),
  };
}

function parseCancelFrame(frame: Record<string, unknown>): CancelFrame {
  requireEnvelope(frame, 'cancel');

  // `intentional: true` is fixed by the contract, and this check is the reason a
  // cancellation cannot be confused with a fault. An abort that arrived without
  // asserted intent would be indistinguishable from a bug terminating the
  // client's work, and the retry classifier would treat it as transient and
  // start another attempt — the opposite of cancelling.
  if (frame['intentional'] !== true) {
    throw new ProtocolViolation('cancel frame must assert intentional: true');
  }

  const result: CancelFrame = {
    protocol_version: PROTOCOL_VERSION,
    type: 'cancel',
    request_id: frame['request_id'] as string,
    task_id: frame['task_id'] as string,
    command_id: requireString(frame, 'command_id', 'cancel frame', { pattern: UUID4 }),
    intentional: true,
  };
  if (frame['reason'] !== undefined) {
    result.reason = requireString(frame, 'reason', 'cancel frame', { min: 0, max: 1000 });
  }
  return result;
}

/**
 * Validate a child-to-host frame before it is written.
 *
 * Outbound validation is not redundant with the host's inbound validation. The
 * host refusing a bad frame fails the run; this refuses it before it reaches the
 * wire, so a mistake in this package is caught at its source with the field
 * named, and a forbidden field never physically leaves the process.
 */
export function assertChildFrame(frame: ChildFrame): void {
  const record = frame as unknown as Record<string, unknown>;
  const forbidden: string[][] = [[...FORBIDDEN.credential]];
  if (frame.type === 'progress') {
    forbidden.push([...FORBIDDEN.reasoning]);
  } else if (frame.type === 'model.request') {
    forbidden.push([...FORBIDDEN.binding]);
  } else if (frame.type === 'result') {
    forbidden.push([...FORBIDDEN.hostLedger]);
  }
  requireEnvelope(record, frame.type, forbidden);

  switch (frame.type) {
    case 'ready': {
      if (
        !Array.isArray(frame.capabilities) ||
        frame.capabilities.length < 1 ||
        frame.capabilities.some((c) => c !== 'input' && c !== 'cancel')
      ) {
        throw new ProtocolViolation('ready frame must advertise only implemented input/cancel capabilities');
      }
      return;
    }
    case 'progress': {
      requireString(record, 'report_id', 'progress frame', { pattern: UUID4 });
      requireString(record, 'message', 'progress frame', { max: 4000 });
      if (frame.stage !== undefined) {
        const stages: readonly string[] = ['evidence_inventory', 'analysis', 'clarification', 'synthesis'];
        if (!stages.includes(frame.stage)) {
          throw new ProtocolViolation('progress frame stage is not a contract stage');
        }
      }
      if (frame.producer_timestamp !== undefined) {
        requireString(record, 'producer_timestamp', 'progress frame', { pattern: RFC3339_UTC });
      }
      const bytes = Buffer.byteLength(JSON.stringify(frame), 'utf8');
      if (bytes > MAX_PROGRESS_EVENT_BYTES) {
        throw new ProtocolViolation(
          `progress frame of ${bytes} bytes exceeds the ${MAX_PROGRESS_EVENT_BYTES}-byte bound`,
        );
      }
      return;
    }
    case 'model.request': {
      requireString(record, 'turn_id', 'model.request frame', { pattern: UUID4 });
      if (!Array.isArray(frame.messages) || frame.messages.length < 1) {
        throw new ProtocolViolation('model.request frame requires at least one message');
      }
      if (frame.max_tokens !== undefined) {
        requireInteger(record, 'max_tokens', 'model.request frame', 1, 10000);
      }
      if (frame.system !== undefined) {
        requireString(record, 'system', 'model.request frame', { max: 16000 });
      }
      return;
    }
    case 'input.required': {
      requireString(record, 'input_request_id', 'input.required frame', { pattern: UUID4 });
      requireString(record, 'prompt', 'input.required frame', { max: 4000 });
      return;
    }
    case 'result': {
      assertInvestigatorReport(frame.report);
      return;
    }
    case 'cancelled': {
      requireString(record, 'command_id', 'cancelled frame', { pattern: UUID4 });
      if (
        frame.partial_findings !== undefined &&
        frame.partial_findings !== null &&
        (!Number.isInteger(frame.partial_findings) || frame.partial_findings < 0)
      ) {
        throw new ProtocolViolation('cancelled frame partial_findings must be a non-negative integer or null');
      }
      return;
    }
    case 'error': {
      const codes: readonly string[] = [
        'invalid_agent_output',
        'protocol_violation',
        'process_failed',
        'deadline_exceeded',
        'model_outcome_unknown',
      ];
      if (!codes.includes(frame.code)) {
        throw new ProtocolViolation('error frame code is not a contract failure code');
      }
      requireString(record, 'message', 'error frame', { max: 1000 });
      return;
    }
  }
}

/**
 * Validate a report against the contract's fixed shape.
 *
 * Exported separately from {@link assertChildFrame} because the synthesis stage
 * checks a candidate report *before* deciding to emit it: a report that fails
 * here is a run that must fail, and discovering that only at write time would
 * leave no opportunity to report the failure honestly.
 */
export function assertInvestigatorReport(report: unknown): asserts report is InvestigatorReport {
  if (!isPlainObject(report)) {
    throw new ProtocolViolation('report must be a JSON object');
  }
  const allowed = new Set(['summary', 'findings', 'uncertainties', 'recommendations', 'evidence_refs', 'documents']);
  if (report.documents !== undefined) {
    if (!Array.isArray(report.documents) || report.documents.length > 4) throw new ProtocolViolation('invalid report documents');
    for (const document of report.documents) {
      if (!isPlainObject(document) || Object.keys(document).sort().join(',') !== 'content,media_type,name' ||
          typeof document.name !== 'string' || !/^[a-z][a-z0-9-]{0,63}\.json$/.test(document.name) || document.media_type !== 'application/json' ||
          typeof document.content !== 'string' || Buffer.byteLength(document.content) > 32768) throw new ProtocolViolation('invalid report document');
      try { const value: unknown = JSON.parse(document.content); if (!isPlainObject(value)) throw new Error(); }
      catch { throw new ProtocolViolation('report document must contain a JSON object'); }
    }
  }
  for (const key of Object.keys(report)) {
    if (!allowed.has(key)) {
      throw new ProtocolViolation(`report carries unknown field ${key}`);
    }
  }
  requireString(report, 'summary', 'report', { max: 4000 });

  const findings = report['findings'];
  if (!Array.isArray(findings)) {
    throw new ProtocolViolation('report findings must be an array');
  }
  for (const finding of findings) {
    if (!isPlainObject(finding)) {
      throw new ProtocolViolation('each finding must be an object');
    }
    for (const key of Object.keys(finding)) {
      if (key !== 'statement' && key !== 'evidence_refs' && key !== 'confidence') {
        throw new ProtocolViolation(`finding carries unknown field ${key}`);
      }
    }
    requireString(finding, 'statement', 'finding', { max: 2000 });
    const refs = finding['evidence_refs'];
    // The contract's minItems: 1. This is the rule that keeps an unsupported
    // claim out of findings entirely — it belongs in uncertainties instead.
    if (!Array.isArray(refs) || refs.length < 1) {
      throw new ProtocolViolation('each finding requires at least one evidence_ref');
    }
    for (const ref of refs) {
      if (typeof ref !== 'string' || ref.length < 1 || ref.length > 256) {
        throw new ProtocolViolation('each finding evidence_ref must be a 1-256 character string');
      }
    }
    if (finding['confidence'] !== undefined) {
      const levels: readonly string[] = ['low', 'medium', 'high'];
      if (!levels.includes(finding['confidence'] as string)) {
        throw new ProtocolViolation('finding confidence must be low, medium or high');
      }
    }
  }

  for (const field of ['uncertainties', 'recommendations'] as const) {
    const values = report[field];
    if (!Array.isArray(values)) {
      throw new ProtocolViolation(`report ${field} must be an array`);
    }
    for (const value of values) {
      if (typeof value !== 'string' || value.length < 1 || value.length > 1000) {
        throw new ProtocolViolation(`each ${field} entry must be a 1-1000 character string`);
      }
    }
  }

  const evidenceRefs = report['evidence_refs'];
  if (!Array.isArray(evidenceRefs)) {
    throw new ProtocolViolation('report evidence_refs must be an array');
  }
  for (const entry of evidenceRefs) {
    if (!isPlainObject(entry)) {
      throw new ProtocolViolation('each evidence_ref must be an object');
    }
    for (const key of Object.keys(entry)) {
      if (key !== 'ref' && key !== 'source' && key !== 'artifact_id') {
        throw new ProtocolViolation(`evidence_ref carries unknown field ${key}`);
      }
    }
    requireString(entry, 'ref', 'evidence_ref', { max: 256 });
    const sources: readonly string[] = ['inputs', 'artifact', 'instructions', 'follow_up_input'];
    if (!sources.includes(entry['source'] as string)) {
      throw new ProtocolViolation('evidence_ref source must be a caller-supplied provenance');
    }
    if (entry['artifact_id'] !== undefined) {
      requireString(entry, 'artifact_id', 'evidence_ref', { pattern: ARTIFACT_ID });
    }
  }
}

/** Serialize a validated child frame as one protocol line, newline included. */
export function encodeChildFrame(frame: ChildFrame): string {
  assertChildFrame(frame);
  const line = JSON.stringify(frame);
  const bytes = Buffer.byteLength(line, 'utf8');
  if (bytes + 1 > MAX_FRAME_BYTES) {
    throw new ProtocolViolation(`child frame of ${bytes} bytes exceeds the ${MAX_FRAME_BYTES}-byte bound`);
  }
  return `${line}\n`;
}
