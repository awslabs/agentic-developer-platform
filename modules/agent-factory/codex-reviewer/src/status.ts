import { DynamoDBClient, UpdateItemCommand } from "@aws-sdk/client-dynamodb";
import type { CodexReviewEnvelope } from "./contracts.js";

const MAX_DETAIL = 1024;

export class InvocationStatus {
  constructor(
    private readonly tableName = process.env.WEBHOOK_EVENTS_TABLE ?? "",
    private readonly client = new DynamoDBClient({}),
  ) {}

  async update(
    envelope: CodexReviewEnvelope,
    status: "in_progress" | "complete" | "failed",
    options: { summary?: string; error?: string; runId?: string } = {},
  ): Promise<void> {
    if (!this.tableName) return;
    const names: Record<string, string> = {
      "#status": "status",
      "#updated": "status_updated_at",
    };
    const values: Record<string, { S: string }> = {
      ":status": { S: status },
      ":updated": { S: new Date().toISOString().replace(/\.\d{3}Z$/, "Z") },
    };
    const assignments = ["#status = :status", "#updated = :updated"];
    const optional = [
      ["summary", options.summary],
      ["error_message", options.error],
      ["run_id", options.runId],
    ] as const;
    for (const [field, raw] of optional) {
      if (!raw) continue;
      const key = field.replace(/_([a-z])/g, (_, letter: string) => letter.toUpperCase());
      names[`#${key}`] = field;
      values[`:${key}`] = { S: raw.slice(0, MAX_DETAIL) };
      assignments.push(`#${key} = :${key}`);
    }
    await this.client.send(
      new UpdateItemCommand({
        TableName: this.tableName,
        Key: {
          event_id: { S: envelope.message_id },
          arrived_at: { S: envelope.arrived_at },
        },
        UpdateExpression: `SET ${assignments.join(", ")}`,
        ExpressionAttributeNames: names,
        ExpressionAttributeValues: values,
        ConditionExpression: "attribute_exists(event_id)",
      }),
    );
  }
}
