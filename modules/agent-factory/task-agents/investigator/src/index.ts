#!/usr/bin/env node
/**
 * Entrypoint for `agent-task-investigator` — Task API T5 (#5798).
 *
 * Invoked by the worker host as:
 *
 *     node /app/task-agents/investigator/dist/index.js --embedded
 *
 * The `--embedded` flag is the command-allowlist entry from design section 3.
 * Registering that command in the host's allowlist is T4's change (#5797) to
 * `agent-worker-image/entrypoint.py`; this package owns the binary it points at.
 * See `README.md` for the exact mapping T4 needs.
 *
 * ## Why stdout discipline is enforced here
 *
 * Stdout is protocol-only. A stray `console.log` anywhere in the dependency tree
 * would inject a non-frame line into the stream and fail the run on a protocol
 * violation — with a cause that is miserable to find, because the guilty line
 * looks like ordinary debug output. So `console.log` is rebound to stderr at
 * startup rather than trusted not to be called. Diagnostics belong on bounded
 * sanitized stderr, which is what the design says stderr is for.
 */

import { createInterface } from 'node:readline';

import { TaskControlAdapter, isControlCancellation, type ControlInput } from './control.js';
import {
  investigate,
  ModelOutcomeUnknownError,
  ModelRejectedError,
  type HostBridge,
  type ModelOutcome,
} from './investigator.js';
import { InvalidAgentOutputError } from './report.js';
import {
  encodeChildFrame,
  IMPLEMENTED_CAPABILITIES,
  parseHostFrame,
  ProtocolViolation,
  type ChildErrorCode,
  type ChildFrame,
  type HostFrame,
  type StartFrame,
  type Stage,
} from './protocol.js';

/** Bounded stderr diagnostic. Never carries frame content or caller evidence. */
function diagnostic(message: string): void {
  const line = message.length > 500 ? `${message.slice(0, 499)}…` : message;
  process.stderr.write(`[agent-task-investigator] ${line}\n`);
}

/**
 * Deterministic id generation for frames the child originates.
 *
 * `randomUUID` from `node:crypto` is a built-in, so this adds no dependency.
 */
function newId(): string {
  // Imported lazily so the module graph stays free of crypto for pure-logic tests.
  return (globalThis.crypto as Crypto).randomUUID();
}

/** An RFC3339 UTC timestamp with second precision, as the contract's pattern requires. */
function nowTimestamp(): string {
  return `${new Date().toISOString().slice(0, 19)}Z`;
}

/**
 * The live host bridge: one JSON frame per line over real stdio.
 *
 * Correlates responses to requests by turn id / input request id rather than by
 * arrival order, because the contract does not promise the host interleaves
 * nothing between a request and its reply.
 */
class StdioHost implements HostBridge {
  private readonly pendingModel = new Map<
    string,
    {
      resolve: (outcome: ModelOutcome) => void;
      reject: (error: Error) => void;
    }
  >();
  private readonly pendingInput = new Map<string, (input: ControlInput | null) => void>();
  private reportSequence = 0;

  constructor(
    private readonly start: StartFrame,
    private readonly control: TaskControlAdapter,
    private readonly write: (frame: ChildFrame) => void,
  ) {}

  async progress(message: string, stage: Stage): Promise<void> {
    this.reportSequence += 1;
    this.write({
      protocol_version: 1,
      type: 'progress',
      request_id: newId(),
      task_id: this.start.task_id,
      report_id: newId(),
      message,
      stage,
      producer_timestamp: nowTimestamp(),
    });
    // Resolves as soon as the frame is written. The contract requires the host
    // forward progress immediately and does not make report.ack a precondition
    // for continuing, so waiting for an ack here would stall work behind the
    // host's durable write.
  }

  async model(request: { messages: unknown[]; system: string; maxTokens: number }): Promise<ModelOutcome> {
    const turnId = newId();
    return await new Promise<ModelOutcome>((resolve, reject) => {
      this.pendingModel.set(turnId, { resolve, reject });
      this.write({
        protocol_version: 1,
        type: 'model.request',
        request_id: newId(),
        task_id: this.start.task_id,
        turn_id: turnId,
        messages: request.messages,
        max_tokens: request.maxTokens,
        system: request.system,
      });
    });
  }

  async askCaller(prompt: string): Promise<ControlInput | null> {
    const inputRequestId = newId();
    return await new Promise<ControlInput | null>((resolve) => {
      this.pendingInput.set(inputRequestId, resolve);
      this.write({
        protocol_version: 1,
        type: 'input.required',
        request_id: newId(),
        task_id: this.start.task_id,
        input_request_id: inputRequestId,
        prompt,
      });
    });
  }

  /** Route a host frame to whatever is waiting for it. */
  accept(frame: HostFrame): void {
    switch (frame.type) {
      case 'model.result': {
        const pending = this.pendingModel.get(frame.turn_id);
        if (pending === undefined) {
          diagnostic(`model.result for an unknown turn was ignored`);
          return;
        }
        this.pendingModel.delete(frame.turn_id);
        if (frame.operation_status === 'unknown') {
          pending.resolve({ status: 'unknown' });
        } else if (frame.operation_status === 'rejected') {
          pending.resolve({
            status: 'rejected',
            code: frame.error_code ?? 'model_access_denied',
            message: 'the host refused the model operation',
          });
        } else if (frame.operation_status === 'confirmed') {
          pending.resolve({ status: 'confirmed', text: textOf(frame.content) });
        }
        // `pending` is not terminal: the host will send a further model.result
        // for this turn, so the request stays outstanding deliberately.
        else {
          this.pendingModel.set(frame.turn_id, pending);
        }
        return;
      }
      case 'turn': {
        // Follow-up input. Each command must be queued exactly once, so there is
        // one queueing path per message rather than two: either it answers an
        // outstanding clarification — in which case the waiting investigation
        // admits it — or nothing is waiting and it is admitted here. Doing both
        // would rely on the adapter's replay rejection to undo a double-queue,
        // which makes exactly-once an accident rather than a property.
        for (const message of frame.messages) {
          const input: ControlInput = {
            kind: 'steering',
            text: message.text,
            command_id: message.command_id,
          };

          const replyTo = message.reply_to;
          const key = replyTo !== undefined && this.pendingInput.has(replyTo)
            ? replyTo
            : firstKey(this.pendingInput);
          const waiter = key !== undefined ? this.pendingInput.get(key) : undefined;

          if (key !== undefined && waiter !== undefined) {
            this.pendingInput.delete(key);
            waiter(input);
            continue;
          }

          const admitted = this.control.admit(input);
          if (admitted !== 'delivered') {
            diagnostic(`follow-up command was not admitted: ${admitted}`);
          }
        }
        return;
      }
      case 'cancel': {
        this.control.cancel(frame.reason, frame.command_id);
        const cancellation = this.control.cancellation();
        if (cancellation !== null) {
          for (const [turnId, pending] of this.pendingModel) {
            this.pendingModel.delete(turnId);
            pending.reject(cancellation);
          }
        }
        // Unblock anything waiting, so the run reaches its cancellation path
        // rather than sitting on a promise until the deadline.
        for (const [key, resolve] of this.pendingInput) {
          this.pendingInput.delete(key);
          resolve(null);
        }
        return;
      }
      case 'report.ack':
        return;
      case 'start':
        diagnostic('a second start frame was ignored');
        return;
    }
  }
}

function firstKey<K, V>(map: Map<K, V>): K | undefined {
  for (const key of map.keys()) {
    return key;
  }
  return undefined;
}

/** Concatenate the text blocks of an Anthropic Messages content array. */
export function textOf(content: unknown[] | null | undefined): string {
  if (!Array.isArray(content)) {
    return '';
  }
  return content
    .map((block) => {
      if (typeof block === 'string') {
        return block;
      }
      if (typeof block === 'object' && block !== null) {
        const text = (block as { text?: unknown }).text;
        return typeof text === 'string' ? text : '';
      }
      return '';
    })
    .join('');
}

/** Map a thrown error to the contract's failure code for the `error` frame. */
export function failureCodeFor(error: unknown): ChildErrorCode {
  if (error instanceof ModelOutcomeUnknownError) {
    return 'model_outcome_unknown';
  }
  if (error instanceof InvalidAgentOutputError) {
    return 'invalid_agent_output';
  }
  if (error instanceof ProtocolViolation) {
    return 'protocol_violation';
  }
  if (error instanceof ModelRejectedError) {
    // A refused grant is a failed run, not malformed output.
    return 'process_failed';
  }
  return 'process_failed';
}

/**
 * Safe message for an error frame.
 *
 * Only this package's own error text is forwarded. Caller evidence and model
 * output never travel here: an error message is persisted and shown to an
 * operator, and echoing analysed content into it would turn a diagnostic surface
 * into an exfiltration path for whatever the model happened to be holding.
 */
export function safeMessageFor(error: unknown): string {
  const raw =
    error instanceof ModelOutcomeUnknownError ||
    error instanceof InvalidAgentOutputError ||
    error instanceof ProtocolViolation ||
    error instanceof ModelRejectedError
      ? error.message
      : 'the task agent failed before producing a report';
  return raw.length > 1000 ? `${raw.slice(0, 999)}…` : raw;
}

async function main(): Promise<number> {
  // Rebound before anything else can call it. See the stdout note above.
  console.log = (...args: unknown[]) => diagnostic(args.map(String).join(' '));
  console.info = console.log;
  console.debug = console.log;

  const stdout = process.stdout;
  const write = (frame: ChildFrame): void => {
    stdout.write(encodeChildFrame(frame));
  };

  const control = new TaskControlAdapter();
  let start: StartFrame | null = null;
  let host: StdioHost | null = null;
  let finished = false;

  const lines = createInterface({ input: process.stdin, crlfDelay: Infinity });

  // The run is one promise settled by the first terminal condition, so a
  // cancellation arriving mid-investigation ends the process on its cancellation
  // path rather than after the investigation happens to finish.
  return await new Promise<number>((resolveExit) => {
    const finish = (code: number): void => {
      if (!finished) {
        finished = true;
        lines.close();
        process.stdin.destroy();
        resolveExit(code);
      }
    };

    const fail = (error: unknown): void => {
      if (finished) {
        return;
      }
      if (isControlCancellation(error)) {
        const cancellation = control.cancellation();
        write({
          protocol_version: 1,
          type: 'cancelled',
          request_id: newId(),
          task_id: (start as StartFrame).task_id,
          command_id: cancellation?.commandId ?? newId(),
          partial_findings: null,
        });
        finish(0);
        return;
      }
      if (start !== null) {
        write({
          protocol_version: 1,
          type: 'error',
          request_id: newId(),
          task_id: start.task_id,
          code: failureCodeFor(error),
          message: safeMessageFor(error),
        });
      } else {
        diagnostic(`failed before start: ${safeMessageFor(error)}`);
      }
      finish(1);
    };

    lines.on('line', (line: string) => {
      const trimmed = line.trim();
      if (trimmed.length === 0 || finished) {
        return;
      }

      let frame: HostFrame;
      try {
        frame = parseHostFrame(line);
      } catch (error) {
        // A frame this build cannot validate is a protocol violation, reported
        // and then terminal: continuing would mean acting on a stream whose
        // meaning is no longer certain.
        fail(error);
        return;
      }

      if (frame.type === 'start') {
        if (start !== null) {
          diagnostic('a second start frame was ignored');
          return;
        }
        start = frame;
        const bridge = new StdioHost(frame, control, write);
        host = bridge;

        write({
          protocol_version: 1,
          type: 'ready',
          request_id: newId(),
          task_id: frame.task_id,
          capabilities: [...IMPLEMENTED_CAPABILITIES],
        });

        void investigate(frame, bridge, control)
          .then((outcome) => {
            if (finished) {
              return;
            }
            write({
              protocol_version: 1,
              type: 'result',
              request_id: newId(),
              task_id: frame.task_id,
              report: outcome.report,
            });
            finish(0);
          })
          .catch(fail);
        return;
      }

      if (host === null) {
        fail(new ProtocolViolation(`received a ${frame.type} frame before start`));
        return;
      }
      if (frame.task_id !== (start as StartFrame).task_id) {
        // Identity mismatch fails the run (design section 8).
        fail(new ProtocolViolation(`frame task_id does not match the started task`));
        return;
      }
      host.accept(frame);
    });

    lines.on('close', () => {
      if (!finished) {
        // Stdin closed without a terminal frame: the host went away. Exiting
        // non-zero is honest — no valid final result was produced.
        diagnostic('host closed the protocol stream before the run produced a result');
        finish(1);
      }
    });
  });
}

// Only run when executed as the entrypoint, so tests may import the helpers above.
if (process.argv[1] !== undefined && import.meta.url === `file://${process.argv[1]}`) {
  main().then(
    (code) => {
      process.exitCode = code;
    },
    (error: unknown) => {
      diagnostic(`unhandled failure: ${String(error)}`);
      process.stdin.destroy();
      process.exitCode = 1;
    },
  );
}
