/**
 * Contract tests for AG-UI event types.
 *
 * Issue #97 Phase 2: validates that event objects produced by the worker
 * conform to the AG-UI protocol spec. These tests don't hit the network —
 * they verify the shape of events against required fields.
 */

import { describe, it, expect } from 'vitest';
import {
  AgUiEventType,
  agUiTimestamp,
  agUiId,
  type RunStartedEvent,
  type RunFinishedEvent,
  type RunErrorEvent,
  type TextMessageStartEvent,
  type TextMessageContentEvent,
  type TextMessageEndEvent,
  type ToolCallStartEvent,
  type ToolCallArgsEvent,
  type ToolCallEndEvent,
  type StateDeltaEvent,
  type StateSnapshotEvent,
  type StepStartedEvent,
  type StepFinishedEvent,
  type CustomEvent,
  type AgUiEvent,
} from './ag-ui-events';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Validate that an event has the required base fields. */
function assertBaseFields(event: AgUiEvent): void {
  expect(event.event_type).toBeDefined();
  expect(typeof event.event_type).toBe('string');
  // event_type must be one of the enum values
  expect(Object.values(AgUiEventType)).toContain(event.event_type);
}

/** Validate that an event round-trips through JSON cleanly. */
function assertSerializable(event: AgUiEvent): void {
  const json = JSON.stringify(event);
  const parsed = JSON.parse(json);
  expect(parsed.event_type).toBe(event.event_type);
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('AG-UI event types', () => {
  describe('AgUiEventType enum', () => {
    it('contains all required lifecycle events', () => {
      expect(AgUiEventType.RUN_STARTED).toBe('RUN_STARTED');
      expect(AgUiEventType.RUN_FINISHED).toBe('RUN_FINISHED');
      expect(AgUiEventType.RUN_ERROR).toBe('RUN_ERROR');
    });

    it('contains all required text message events', () => {
      expect(AgUiEventType.TEXT_MESSAGE_START).toBe('TEXT_MESSAGE_START');
      expect(AgUiEventType.TEXT_MESSAGE_CONTENT).toBe('TEXT_MESSAGE_CONTENT');
      expect(AgUiEventType.TEXT_MESSAGE_END).toBe('TEXT_MESSAGE_END');
    });

    it('contains all required tool call events', () => {
      expect(AgUiEventType.TOOL_CALL_START).toBe('TOOL_CALL_START');
      expect(AgUiEventType.TOOL_CALL_ARGS).toBe('TOOL_CALL_ARGS');
      expect(AgUiEventType.TOOL_CALL_END).toBe('TOOL_CALL_END');
    });

    it('contains state management events', () => {
      expect(AgUiEventType.STATE_DELTA).toBe('STATE_DELTA');
      expect(AgUiEventType.STATE_SNAPSHOT).toBe('STATE_SNAPSHOT');
    });
  });

  describe('helper functions', () => {
    it('agUiTimestamp returns ISO-8601 string', () => {
      const ts = agUiTimestamp();
      expect(ts).toMatch(/^\d{4}-\d{2}-\d{2}T/);
      expect(() => new Date(ts)).not.toThrow();
    });

    it('agUiId generates prefixed unique IDs', () => {
      const id1 = agUiId('msg');
      const id2 = agUiId('msg');
      expect(id1).toMatch(/^msg_/);
      expect(id2).toMatch(/^msg_/);
      expect(id1).not.toBe(id2);

      const tcId = agUiId('tc');
      expect(tcId).toMatch(/^tc_/);
    });
  });

  describe('RunStartedEvent', () => {
    it('conforms to AG-UI spec', () => {
      const event: RunStartedEvent = {
        event_type: AgUiEventType.RUN_STARTED,
        threadId: 'session-123',
        runId: 'task-456',
        timestamp: agUiTimestamp(),
      };
      assertBaseFields(event);
      assertSerializable(event);
      expect(event.threadId).toBe('session-123');
      expect(event.runId).toBe('task-456');
    });
  });

  describe('RunFinishedEvent', () => {
    it('conforms to AG-UI spec', () => {
      const event: RunFinishedEvent = {
        event_type: AgUiEventType.RUN_FINISHED,
        threadId: 'session-123',
        runId: 'task-456',
        result: { tokens: { input: 100, output: 50 } },
        timestamp: agUiTimestamp(),
      };
      assertBaseFields(event);
      assertSerializable(event);
      expect(event.result).toEqual({ tokens: { input: 100, output: 50 } });
    });

    it('works without optional result', () => {
      const event: RunFinishedEvent = {
        event_type: AgUiEventType.RUN_FINISHED,
        threadId: 'session-123',
        runId: 'task-456',
      };
      assertBaseFields(event);
      expect(event.result).toBeUndefined();
    });
  });

  describe('RunErrorEvent', () => {
    it('conforms to AG-UI spec', () => {
      const event: RunErrorEvent = {
        event_type: AgUiEventType.RUN_ERROR,
        message: 'Model overloaded',
        code: 'OVERLOADED',
        timestamp: agUiTimestamp(),
      };
      assertBaseFields(event);
      assertSerializable(event);
      expect(event.message).toBe('Model overloaded');
      expect(event.code).toBe('OVERLOADED');
    });
  });

  describe('TextMessageStartEvent', () => {
    it('conforms to AG-UI spec', () => {
      const event: TextMessageStartEvent = {
        event_type: AgUiEventType.TEXT_MESSAGE_START,
        messageId: agUiId('msg'),
        role: 'assistant',
        timestamp: agUiTimestamp(),
      };
      assertBaseFields(event);
      assertSerializable(event);
      expect(event.messageId).toMatch(/^msg_/);
      expect(event.role).toBe('assistant');
    });
  });

  describe('TextMessageContentEvent', () => {
    it('conforms to AG-UI spec with non-empty delta', () => {
      const event: TextMessageContentEvent = {
        event_type: AgUiEventType.TEXT_MESSAGE_CONTENT,
        messageId: agUiId('msg'),
        delta: 'Hello world',
        timestamp: agUiTimestamp(),
      };
      assertBaseFields(event);
      assertSerializable(event);
      expect(event.delta).toBe('Hello world');
      expect(event.delta.length).toBeGreaterThan(0);
    });
  });

  describe('TextMessageEndEvent', () => {
    it('conforms to AG-UI spec', () => {
      const event: TextMessageEndEvent = {
        event_type: AgUiEventType.TEXT_MESSAGE_END,
        messageId: agUiId('msg'),
        timestamp: agUiTimestamp(),
      };
      assertBaseFields(event);
      assertSerializable(event);
    });
  });

  describe('ToolCallStartEvent', () => {
    it('conforms to AG-UI spec', () => {
      const event: ToolCallStartEvent = {
        event_type: AgUiEventType.TOOL_CALL_START,
        toolCallId: agUiId('tc'),
        toolCallName: 'WebSearch',
        parentMessageId: agUiId('msg'),
        timestamp: agUiTimestamp(),
      };
      assertBaseFields(event);
      assertSerializable(event);
      expect(event.toolCallName).toBe('WebSearch');
    });
  });

  describe('ToolCallArgsEvent', () => {
    it('conforms to AG-UI spec', () => {
      const event: ToolCallArgsEvent = {
        event_type: AgUiEventType.TOOL_CALL_ARGS,
        toolCallId: agUiId('tc'),
        delta: '{"query": "React hooks"}',
        timestamp: agUiTimestamp(),
      };
      assertBaseFields(event);
      assertSerializable(event);
    });
  });

  describe('ToolCallEndEvent', () => {
    it('conforms to AG-UI spec', () => {
      const event: ToolCallEndEvent = {
        event_type: AgUiEventType.TOOL_CALL_END,
        toolCallId: agUiId('tc'),
        timestamp: agUiTimestamp(),
      };
      assertBaseFields(event);
      assertSerializable(event);
    });
  });

  describe('StateDeltaEvent', () => {
    it('conforms to AG-UI spec with RFC 6902 ops', () => {
      const event: StateDeltaEvent = {
        event_type: AgUiEventType.STATE_DELTA,
        delta: [
          { op: 'replace', path: '/tokens', value: { input: 100, output: 50 } },
          { op: 'add', path: '/turnCount', value: 3 },
          { op: 'remove', path: '/staleField' },
        ],
        timestamp: agUiTimestamp(),
      };
      assertBaseFields(event);
      assertSerializable(event);
      expect(event.delta).toHaveLength(3);
      expect(event.delta[0].op).toBe('replace');
    });

    it('supports heartbeat pattern', () => {
      const event: StateDeltaEvent = {
        event_type: AgUiEventType.STATE_DELTA,
        delta: [
          { op: 'replace', path: '/heartbeat', value: { turn: 2, ts: Date.now() } },
        ],
      };
      assertBaseFields(event);
      assertSerializable(event);
    });
  });

  describe('StateSnapshotEvent', () => {
    it('conforms to AG-UI spec', () => {
      const event: StateSnapshotEvent = {
        event_type: AgUiEventType.STATE_SNAPSHOT,
        snapshot: { tokens: { input: 100, output: 50 }, turnCount: 3 },
        timestamp: agUiTimestamp(),
      };
      assertBaseFields(event);
      assertSerializable(event);
    });
  });

  describe('StepStartedEvent', () => {
    it('conforms to AG-UI spec', () => {
      const event: StepStartedEvent = {
        event_type: AgUiEventType.STEP_STARTED,
        stepName: 'context_assembly',
        timestamp: agUiTimestamp(),
      };
      assertBaseFields(event);
      assertSerializable(event);
    });
  });

  describe('StepFinishedEvent', () => {
    it('conforms to AG-UI spec', () => {
      const event: StepFinishedEvent = {
        event_type: AgUiEventType.STEP_FINISHED,
        stepName: 'context_assembly',
        timestamp: agUiTimestamp(),
      };
      assertBaseFields(event);
      assertSerializable(event);
    });
  });

  describe('CustomEvent', () => {
    it('conforms to AG-UI spec', () => {
      const event: CustomEvent = {
        event_type: AgUiEventType.CUSTOM,
        name: 'legacy_progress',
        value: { kind: 'heartbeat', turn: 5 },
        timestamp: agUiTimestamp(),
      };
      assertBaseFields(event);
      assertSerializable(event);
    });
  });

  describe('full event session simulation', () => {
    it('validates a complete agent session event log', () => {
      const sessionId = 'session-test';
      const taskId = 'task-test';
      const msgId = agUiId('msg');
      const tcId = agUiId('tc');

      const events: AgUiEvent[] = [
        // 1. Run starts
        {
          event_type: AgUiEventType.RUN_STARTED,
          threadId: sessionId,
          runId: taskId,
          timestamp: agUiTimestamp(),
        },
        // 2. Tool call
        {
          event_type: AgUiEventType.TOOL_CALL_START,
          toolCallId: tcId,
          toolCallName: 'WebSearch',
          parentMessageId: msgId,
          timestamp: agUiTimestamp(),
        },
        {
          event_type: AgUiEventType.TOOL_CALL_ARGS,
          toolCallId: tcId,
          delta: '{"query": "React hooks best practices"}',
          timestamp: agUiTimestamp(),
        },
        {
          event_type: AgUiEventType.TOOL_CALL_END,
          toolCallId: tcId,
          timestamp: agUiTimestamp(),
        },
        // 3. Heartbeat
        {
          event_type: AgUiEventType.STATE_DELTA,
          delta: [{ op: 'replace', path: '/heartbeat', value: { turn: 1, ts: Date.now() } }],
          timestamp: agUiTimestamp(),
        },
        // 4. Text message
        {
          event_type: AgUiEventType.TEXT_MESSAGE_START,
          messageId: msgId,
          role: 'assistant' as const,
          timestamp: agUiTimestamp(),
        },
        {
          event_type: AgUiEventType.TEXT_MESSAGE_CONTENT,
          messageId: msgId,
          delta: 'Here are the best practices for React hooks...',
          timestamp: agUiTimestamp(),
        },
        {
          event_type: AgUiEventType.TEXT_MESSAGE_END,
          messageId: msgId,
          timestamp: agUiTimestamp(),
        },
        // 5. Final state
        {
          event_type: AgUiEventType.STATE_DELTA,
          delta: [
            { op: 'replace', path: '/tokens', value: { input: 500, output: 200 } },
            { op: 'replace', path: '/turnCount', value: 2 },
          ],
          timestamp: agUiTimestamp(),
        },
        // 6. Run finishes
        {
          event_type: AgUiEventType.RUN_FINISHED,
          threadId: sessionId,
          runId: taskId,
          result: { tokens: { input: 500, output: 200 }, turnCount: 2 },
          timestamp: agUiTimestamp(),
        },
      ];

      // Validate each event
      for (const event of events) {
        assertBaseFields(event);
        assertSerializable(event);
      }

      // Validate sequence: RUN_STARTED first, RUN_FINISHED last
      expect(events[0].event_type).toBe(AgUiEventType.RUN_STARTED);
      expect(events[events.length - 1].event_type).toBe(AgUiEventType.RUN_FINISHED);

      // Validate tool call sequence: START before END
      const tcStartIdx = events.findIndex(e => e.event_type === AgUiEventType.TOOL_CALL_START);
      const tcEndIdx = events.findIndex(e => e.event_type === AgUiEventType.TOOL_CALL_END);
      expect(tcStartIdx).toBeLessThan(tcEndIdx);

      // Validate text message sequence: START before CONTENT before END
      const tmStartIdx = events.findIndex(e => e.event_type === AgUiEventType.TEXT_MESSAGE_START);
      const tmContentIdx = events.findIndex(e => e.event_type === AgUiEventType.TEXT_MESSAGE_CONTENT);
      const tmEndIdx = events.findIndex(e => e.event_type === AgUiEventType.TEXT_MESSAGE_END);
      expect(tmStartIdx).toBeLessThan(tmContentIdx);
      expect(tmContentIdx).toBeLessThan(tmEndIdx);
    });
  });
});
