"""
Bedrock-powered task classifier with message enrichment and thread assignment.

Uses Claude Sonnet to:
1. Determine execution path (direct_response, long_running, github_actions)
2. Select agent persona
3. Extract repo/issue references
4. Enrich task description from conversation context
5. Assign to existing thread or create new one
"""

import json
import logging
import os
from dataclasses import dataclass
from typing import Any

import boto3

logger = logging.getLogger(__name__)

CLASSIFIER_MODEL = os.environ.get(
    "CLASSIFIER_MODEL", "global.anthropic.claude-sonnet-4-6"
)
AWS_REGION = os.environ.get("AWS_REGION_NAME", "us-east-1")

_bedrock_client = None


def _get_bedrock():
    global _bedrock_client
    if _bedrock_client is None:
        _bedrock_client = boto3.client("bedrock-runtime", region_name=AWS_REGION)
    return _bedrock_client


CLASSIFIER_SYSTEM_PROMPT = """You are a task router for ADP (Agentic Developer Platform). Given a user message, conversation history, and active threads, decide the execution path, persona, and thread assignment.

Return ONLY valid JSON:

{
    "path": "direct_response" | "long_running" | "github_actions",
    "persona": "developer" | "architect" | "reviewer" | "operations" | "pm" | "product",
    "repo": "owner/repo" or null,
    "issue_number": integer or null,
    "create_issue": true/false (only when path=github_actions and no existing issue),
    "response": "direct answer" or null (only when path=direct_response),
    "enriched_message": "structured task description" or null (only when path=github_actions),
    "issue_title": "short title" or null (only when create_issue=true),
    "escalation_note": "friendly status message for user" or null (when path != direct_response),
    "thread_action": "new" | "follow_up" | "none",
    "follow_up_thread_id": "thread-id" or null (when thread_action=follow_up),
    "reasoning": "one sentence"
}

Path rules:
- "direct_response": greetings, general knowledge, status checks, simple questions. Answer directly.
- "long_running": multi-turn reasoning, analysis, planning — no git/code needed.
- "github_actions": code changes, PRs, reviews, issue work on a specific repo.

Thread rules:
- "none": for direct_response (no thread needed)
- "new": message is about a new topic unrelated to any active thread
- "follow_up": message is a follow-up to an active thread (set follow_up_thread_id)

For follow-ups to github_actions threads: the message will be posted as a comment on the linked GitHub issue.
For follow-ups to long_running threads: the message will be buffered until the current task completes.

For github_actions, ALWAYS set create_issue=true unless user references an existing issue number.

When enriching (enriched_message), include: clear title, what needs to be done, which components/files, constraints, and acceptance criteria from conversation context."""


@dataclass
class ClassificationResult:
    path: str = "long_running"
    persona: str = "developer"
    repo: str | None = None
    issue_number: int | None = None
    create_issue: bool = False
    response: str | None = None
    enriched_message: str | None = None
    issue_title: str | None = None
    escalation_note: str | None = None
    thread_action: str = "new"  # "new" | "follow_up" | "none"
    follow_up_thread_id: str | None = None
    reasoning: str = ""
    raw: dict | None = None


def classify_message(
    message: str,
    conversation_history: list[dict] | None = None,
    active_threads: list[dict] | None = None,
    channel: str = "",
    user_name: str = "",
) -> ClassificationResult:
    """Classify message with thread awareness."""
    context_parts = [f"Channel: {channel}", f"User: {user_name}"]

    if conversation_history:
        history_text = "\n".join(
            f"  {m.get('role', 'user')}: {m.get('content', '')[:500]}"
            for m in conversation_history[-10:]
        )
        context_parts.append(f"Recent conversation:\n{history_text}")

    if active_threads:
        threads_text = "\n".join(
            f"  - thread_id={t.get('thread_id')}, topic={t.get('topic', '?')}, "
            f"path={t.get('path', '?')}, status={t.get('status', '?')}, "
            f"github_issue={t.get('github_issue_url', 'none')}"
            for t in active_threads
        )
        context_parts.append(f"Active threads:\n{threads_text}")
    else:
        context_parts.append("Active threads: none")

    context = "\n".join(context_parts)
    prompt = f"Context:\n{context}\n\nUser message: {message}"

    try:
        body = json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 2048,
            "system": CLASSIFIER_SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": prompt}],
        })

        response = _get_bedrock().invoke_model(
            modelId=CLASSIFIER_MODEL,
            contentType="application/json",
            accept="application/json",
            body=body,
        )

        result_body = json.loads(response["body"].read())
        text = result_body.get("content", [{}])[0].get("text", "{}")

        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            text = text.rsplit("```", 1)[0]

        result = json.loads(text)

        logger.info(
            "Classification: path=%s persona=%s thread=%s/%s",
            result.get("path"), result.get("persona"),
            result.get("thread_action"), result.get("follow_up_thread_id"),
        )

        return ClassificationResult(
            path=result.get("path", "long_running"),
            persona=result.get("persona", "developer"),
            repo=result.get("repo"),
            issue_number=result.get("issue_number"),
            create_issue=result.get("create_issue", False),
            response=result.get("response"),
            enriched_message=result.get("enriched_message"),
            issue_title=result.get("issue_title"),
            escalation_note=result.get("escalation_note"),
            thread_action=result.get("thread_action", "new"),
            follow_up_thread_id=result.get("follow_up_thread_id"),
            reasoning=result.get("reasoning", ""),
            raw=result,
        )

    except Exception as e:
        logger.error("Classification failed: %s", e)
        return ClassificationResult(path="long_running", reasoning=f"Classification failed: {e}")
