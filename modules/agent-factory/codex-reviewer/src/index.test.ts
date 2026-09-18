import assert from "node:assert/strict";
import test from "node:test";
import {
  DeleteMessageCommand,
  ReceiveMessageCommand,
  type SQSClient,
} from "@aws-sdk/client-sqs";
import { processOne } from "./index.js";
import type { InvocationStatus } from "./status.js";

const envelope = {
  version: "1.0",
  kind: "codex_pr_review",
  message_id: "run-1",
  arrived_at: "2026-09-18T00:00:00Z",
  tenant_id: "tenant-1",
  installation_id: 42,
  repository: "aws-e/adp",
  pull_request: {
    number: 6000,
    issue_number: 5054,
    head_ref: "agent/issue-5054",
    base_ref: "main",
    expected_head_sha: "a".repeat(40),
    html_url: "https://github.com/aws-e/adp/pull/6000",
  },
} as const;

function fakes() {
  const commands: unknown[] = [];
  const states: string[] = [];
  const sqs = {
    async send(command: unknown) {
      commands.push(command);
      if (command instanceof ReceiveMessageCommand) {
        return {
          Messages: [{ Body: JSON.stringify(envelope), ReceiptHandle: "receipt-1" }],
        };
      }
      return {};
    },
  } as unknown as SQSClient;
  const status = {
    async update(_envelope: unknown, state: string) {
      states.push(state);
    },
  } as unknown as InvocationStatus;
  return { sqs, status, commands, states };
}

test("deletes a message only after an independent review succeeds", async () => {
  const { sqs, status, commands, states } = fakes();
  const result = await processOne({
    sqs,
    status,
    queueUrl: "https://sqs.example/reviews.fifo",
    execute: async (parsed) => {
      assert.equal(parsed.pull_request.number, 6000);
      return { status: "approved", sha: parsed.pull_request.expected_head_sha };
    },
  });
  assert.deepEqual(result, { status: "approved", sha: "a".repeat(40) });
  assert.deepEqual(states, ["in_progress", "complete"]);
  assert.equal(commands.some((command) => command instanceof DeleteMessageCommand), true);
});

test("leaves a failed message for SQS retry and DLQ redrive", async () => {
  const { sqs, status, commands, states } = fakes();
  await assert.rejects(
    processOne({
      sqs,
      status,
      queueUrl: "https://sqs.example/reviews.fifo",
      execute: async () => {
        throw new Error("review failed");
      },
    }),
    /review failed/,
  );
  assert.deepEqual(states, ["in_progress", "failed"]);
  assert.equal(commands.some((command) => command instanceof DeleteMessageCommand), false);
});
