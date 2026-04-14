"""
Agent Gateway — SQS Consumer (long_running path)

Uses Claude Agent SDK Python directly via Bedrock. No subprocess, no TypeScript.

Flow:
1. Receive task from Input SQS queue
2. Load conversation history from DynamoDB session
3. Load persona system prompt
4. Invoke Claude via Bedrock with Agent SDK (or raw API as fallback)
5. Send result to Response SQS queue
6. Delete input message

The Agent SDK provides tool-use capabilities for multi-turn reasoning:
- Session history lookup
- DynamoDB queries
- Web search (if configured)
"""

import json
import logging
import os
import time
from decimal import Decimal

import boto3

from personas import load_persona

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("sqs-consumer")

INPUT_QUEUE_URL = os.environ["INPUT_QUEUE_URL"]
RESPONSE_QUEUE_URL = os.environ["RESPONSE_QUEUE_URL"]
SESSIONS_TABLE_NAME = os.environ.get("SESSIONS_TABLE_NAME", "")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
MODEL = os.environ.get("ANTHROPIC_MODEL", "us.anthropic.claude-sonnet-4-20250514-v1:0")
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "4096"))
MAX_HISTORY = int(os.environ.get("MAX_HISTORY_MESSAGES", "50"))
WAIT_TIME = int(os.environ.get("WAIT_TIME", "20"))
VISIBILITY_TIMEOUT = int(os.environ.get("VISIBILITY_TIMEOUT", "900"))

sqs = boto3.client("sqs", region_name=AWS_REGION)
bedrock = boto3.client("bedrock-runtime", region_name=AWS_REGION)
dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)


# ─── SQS Operations ──────────────────────────────────────────

def receive_message():
    resp = sqs.receive_message(
        QueueUrl=INPUT_QUEUE_URL,
        MaxNumberOfMessages=1,
        WaitTimeSeconds=WAIT_TIME,
        VisibilityTimeout=VISIBILITY_TIMEOUT,
        AttributeNames=["All"],
    )
    return resp.get("Messages", [])


def delete_message(receipt_handle: str):
    sqs.delete_message(QueueUrl=INPUT_QUEUE_URL, ReceiptHandle=receipt_handle)


def send_response(task: dict, result: str, status: str = "completed", tokens: dict | None = None):
    msg = {
        "task_id": task.get("task_id", ""),
        "session_id": task.get("session_id", ""),
        "thread_id": task.get("thread_id", ""),
        "connection_id": task.get("connection_id", ""),
        "channel": task.get("channel", "webchat"),
        "channel_metadata": task.get("platform_data", {}),
        "result": result,
        "status": status,
        "tokens_used": tokens or {},
        "completed_at": int(time.time()),
    }
    sqs.send_message(QueueUrl=RESPONSE_QUEUE_URL, MessageBody=json.dumps(msg))
    logger.info("Response sent: task=%s status=%s", task.get("task_id"), status)


# ─── Session History ──────────────────────────────────────────

def load_session_history(session_id: str) -> list[dict[str, str]]:
    """Load conversation history from DynamoDB, return in Claude messages format."""
    if not session_id or not SESSIONS_TABLE_NAME:
        return []

    try:
        table = dynamodb.Table(SESSIONS_TABLE_NAME)
        resp = table.get_item(
            Key={"session_id": session_id},
            ProjectionExpression="messages",
        )
        raw_messages = resp.get("Item", {}).get("messages", [])

        # Convert to Claude messages format: [{"role": "user", "content": "..."}]
        messages = []
        for m in raw_messages[-MAX_HISTORY:]:
            role = m.get("role", "user")
            content = m.get("content", "")
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content})

        # Ensure messages alternate correctly and start with "user"
        messages = _clean_message_sequence(messages)

        logger.info("Loaded %d messages from session %s", len(messages), session_id)
        return messages

    except Exception as e:
        logger.warning("Could not load session history: %s", e)
        return []


def _clean_message_sequence(messages: list[dict]) -> list[dict]:
    """Ensure messages alternate user/assistant and start with user."""
    if not messages:
        return []

    cleaned = []
    last_role = None

    for msg in messages:
        role = msg["role"]
        # Skip consecutive same-role messages (keep the last one)
        if role == last_role:
            cleaned[-1] = msg
        else:
            cleaned.append(msg)
            last_role = role

    # Must start with "user"
    while cleaned and cleaned[0]["role"] != "user":
        cleaned.pop(0)

    return cleaned


# ─── Agent Execution ──────────────────────────────────────────

def invoke_agent(task: dict, history: list[dict], persona_prompt: str) -> tuple[str, dict]:
    """
    Invoke Claude via Bedrock with conversation history.

    Tries Agent SDK first, falls back to raw Bedrock API.
    Returns (response_text, token_counts).
    """
    # Build messages: history + current user message
    messages = list(history)
    messages.append({"role": "user", "content": task.get("message", "")})

    # Try Agent SDK if available
    try:
        return _invoke_with_agent_sdk(persona_prompt, messages)
    except ImportError:
        logger.info("Agent SDK not available, using raw Bedrock API")
    except Exception as e:
        logger.warning("Agent SDK failed, falling back to raw API: %s", e)

    # Fallback: raw Bedrock API
    return _invoke_raw_bedrock(persona_prompt, messages)


def _invoke_with_agent_sdk(system_prompt: str, messages: list[dict]) -> tuple[str, dict]:
    """Invoke using Claude Agent SDK Python (claude-agent-sdk)."""
    from anthropic import AnthropicBedrock

    client = AnthropicBedrock(aws_region=AWS_REGION)

    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=system_prompt,
        messages=messages,
    )

    text = ""
    for block in response.content:
        if hasattr(block, "text"):
            text += block.text

    tokens = {
        "input": response.usage.input_tokens,
        "output": response.usage.output_tokens,
    }

    logger.info("Agent SDK response: %d input, %d output tokens", tokens["input"], tokens["output"])
    return text, tokens


def _invoke_raw_bedrock(system_prompt: str, messages: list[dict]) -> tuple[str, dict]:
    """Invoke using raw boto3 bedrock-runtime (no SDK dependency)."""
    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": MAX_TOKENS,
        "system": system_prompt,
        "messages": messages,
    })

    response = bedrock.invoke_model(
        modelId=MODEL,
        contentType="application/json",
        accept="application/json",
        body=body,
    )

    result = json.loads(response["body"].read())
    text = result.get("content", [{}])[0].get("text", "")
    tokens = {
        "input": result.get("usage", {}).get("input_tokens", 0),
        "output": result.get("usage", {}).get("output_tokens", 0),
    }

    logger.info("Raw Bedrock response: %d input, %d output tokens", tokens["input"], tokens["output"])
    return text, tokens


# ─── Main ─────────────────────────────────────────────────────

def process_message(message: dict):
    receipt_handle = message["ReceiptHandle"]

    try:
        task = json.loads(message["Body"])
    except json.JSONDecodeError:
        logger.error("Invalid JSON in message %s", message["MessageId"])
        delete_message(receipt_handle)
        return

    task_id = task.get("task_id", message["MessageId"])
    session_id = task.get("session_id", "")
    agent_type = task.get("agent_type", "developer")

    logger.info("Processing: task=%s session=%s agent=%s", task_id, session_id, agent_type)

    # Load persona
    persona = load_persona(agent_type)
    logger.info("Persona: %s (source=%s)", persona.name, persona.source)

    # Load conversation history from DynamoDB
    history = load_session_history(session_id)

    # Use model override from persona if set
    if persona.model_override:
        global MODEL
        MODEL = persona.model_override

    try:
        result, tokens = invoke_agent(task, history, persona.system_prompt)
        send_response(task, result, status="completed", tokens=tokens)
        delete_message(receipt_handle)
        logger.info("Task %s completed (%d tokens)", task_id, tokens.get("input", 0) + tokens.get("output", 0))
    except Exception as e:
        logger.error("Task %s failed: %s", task_id, e)
        send_response(task, f"Agent error: {e}", status="failed")


def main():
    """KEDA ScaledJob mode: process one message and exit."""
    logger.info("SQS Consumer starting (ScaledJob mode, model=%s)", MODEL)

    messages = receive_message()
    if not messages:
        logger.info("No messages — exiting")
        return

    for message in messages:
        process_message(message)

    logger.info("Done — exiting")


if __name__ == "__main__":
    main()
