import {
  ChangeMessageVisibilityCommand,
  DeleteMessageCommand,
  ReceiveMessageCommand,
  SQSClient,
} from "@aws-sdk/client-sqs";
import { setTimeout as delay } from "node:timers/promises";
import { parseEnvelope, type CodexReviewEnvelope } from "./contracts.js";
import { runReview, type ReviewRunResult } from "./reviewer.js";
import { InvocationStatus } from "./status.js";

function resultSummary(result: ReviewRunResult): string {
  switch (result.status) {
    case "stale":
      return `Skipped stale PR head: expected ${result.expected}, current ${result.actual}`;
    case "changes_requested":
      return `Review requested changes with ${result.blockers} blocker(s)`;
    case "fixes_pushed":
      return `Mechanical review fixes pushed at ${result.sha}; awaiting fresh review`;
    case "awaiting_human":
      return `Reviewed ${result.sha}; distinct reviewer identity unavailable, awaiting human approval`;
    case "approved":
      return `Reviewed and approved ${result.sha}; automatic merge disabled`;
    case "merged":
      return `Reviewed and merged ${result.sha} as ${result.mergeSha}`;
  }
}

async function heartbeat(
  sqs: SQSClient,
  queueUrl: string,
  receiptHandle: string,
  visibilitySeconds: number,
  signal: AbortSignal,
): Promise<void> {
  const intervalSeconds = Math.max(15, Math.min(60, Math.floor(visibilitySeconds / 2)));
  while (!signal.aborted) {
    try {
      await delay(intervalSeconds * 1000, undefined, { signal });
      await sqs.send(
        new ChangeMessageVisibilityCommand({
          QueueUrl: queueUrl,
          ReceiptHandle: receiptHandle,
          VisibilityTimeout: visibilitySeconds,
        }),
      );
    } catch (error) {
      if (signal.aborted) return;
      console.error("codex-reviewer visibility heartbeat failed", error);
    }
  }
}

async function updateStatusSafely(
  status: InvocationStatus,
  envelope: CodexReviewEnvelope,
  state: "in_progress" | "complete" | "failed",
  options: { summary?: string; error?: string; runId?: string },
): Promise<void> {
  try {
    await status.update(envelope, state, options);
  } catch (error) {
    console.error(`codex-reviewer could not record ${state} status`, error);
  }
}

export async function processOne(options: {
  sqs?: SQSClient;
  status?: InvocationStatus;
  queueUrl?: string;
  execute?: (envelope: CodexReviewEnvelope) => Promise<ReviewRunResult>;
} = {}): Promise<"empty" | ReviewRunResult> {
  const queueUrl = options.queueUrl ?? process.env.CODEX_REVIEW_QUEUE_URL ?? "";
  if (!queueUrl) throw new Error("CODEX_REVIEW_QUEUE_URL is required");
  const sqs = options.sqs ?? new SQSClient({});
  const status = options.status ?? new InvocationStatus();
  const execute = options.execute ?? runReview;
  const visibilitySeconds = Number(process.env.CODEX_REVIEWER_VISIBILITY_SECONDS ?? 300);
  const received = await sqs.send(
    new ReceiveMessageCommand({
      QueueUrl: queueUrl,
      MaxNumberOfMessages: 1,
      WaitTimeSeconds: 20,
      VisibilityTimeout: visibilitySeconds,
      MessageSystemAttributeNames: ["ApproximateReceiveCount"],
    }),
  );
  const message = received.Messages?.[0];
  if (!message) return "empty";
  if (!message.Body || !message.ReceiptHandle) {
    throw new Error("SQS returned a message without Body or ReceiptHandle");
  }

  const envelope = parseEnvelope(message.Body);
  await updateStatusSafely(status, envelope, "in_progress", {
    runId: process.env.HOSTNAME ?? "agent-codex-reviewer",
  });
  const controller = new AbortController();
  const heartbeatTask = heartbeat(
    sqs,
    queueUrl,
    message.ReceiptHandle,
    visibilitySeconds,
    controller.signal,
  );
  try {
    const result = await execute(envelope);
    await updateStatusSafely(status, envelope, "complete", {
      summary: resultSummary(result),
    });
    await sqs.send(
      new DeleteMessageCommand({
        QueueUrl: queueUrl,
        ReceiptHandle: message.ReceiptHandle,
      }),
    );
    return result;
  } catch (error) {
    await updateStatusSafely(status, envelope, "failed", {
      error: (error as Error).message,
    });
    throw error;
  } finally {
    controller.abort();
    await heartbeatTask;
  }
}

async function main(): Promise<void> {
  if ((process.env.CODEX_REVIEWER_ENABLED ?? "false") !== "true") {
    console.log("agent-codex-reviewer is disabled");
    return;
  }
  const result = await processOne();
  console.log(JSON.stringify(result));
}

if (process.argv[1] && import.meta.url === new URL(`file://${process.argv[1]}`).href) {
  main().catch((error) => {
    console.error("agent-codex-reviewer failed", error);
    process.exitCode = 1;
  });
}
