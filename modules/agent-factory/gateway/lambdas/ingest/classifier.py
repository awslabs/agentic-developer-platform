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
    "escalation_note": "friendly status message shown to the user immediately after they send — REQUIRED when path != direct_response, null when direct_response",
    "thread_action": "new" | "follow_up" | "none",
    "follow_up_thread_id": "thread-id" or null (when thread_action=follow_up),
    "reasoning": "one sentence"
}

# About the paths

The long_running agent has REAL TOOLS. It can execute Bash in /tmp/workspace, Read/Write/Edit files, Grep, WebSearch, WebFetch, and invoke MCP/Skill tools. It can clone repos, inspect code, run scripts, fetch current web pages, and produce real output from actual execution. Route tool-requiring asks there.

The direct_response path has NO tools. You (the classifier) write the whole answer yourself into the "response" field. If you would have to say "I can't do that", "I don't have access to X", "as an AI I cannot…", or you would have to guess at current/external information, then direct_response is the WRONG path.

# Path rules — pick the narrowest that fits

- "direct_response": ONLY for trivial, self-contained replies that need no tools, no fresh information, and fit in 1-2 sentences. Examples: greetings ("hi", "hello"), thanks, name/role questions ("who are you"), yes/no acknowledgements. When in doubt, do NOT use this.
- "long_running": DEFAULT. Multi-turn reasoning, analysis, planning, or any ask that benefits from tool use — running commands, reading/editing files, web search, fetching URLs, summarising external content, producing structured output. This is the right choice whenever direct_response is not clearly appropriate.
- "github_actions": code changes, PRs, reviews, or issue work on a specific repo. Requires a concrete repo reference in the message or an active thread.

## Hard triggers that force long_running

These patterns ALWAYS mean long_running (never direct_response), even for short messages:

- Any question about the current time, current date, "right now", "currently", "today's time", "what time is it" — the agent can run `date` or WebFetch a time API; you cannot.
- Words signalling fresh external data: "trending", "latest", "current", "news", "recent", "this week", "today's", "up to date", "as of".
- Explicit research asks: "research X", "find out about X", "what are the top N ...", "compare X and Y", "tell me about the state of X".
- Any mention of running commands, executing code, inspecting files, browsing the web, cloning a repo, checking a URL.
- Anything the user phrases as a task ("can you do X", "please do Y", "help me with Z") where doing X/Y/Z would involve reading, writing, searching, or executing anything.

If the message matches any of these, set path=long_running regardless of how short or conversational it sounds.

# Response-field rules

When path="direct_response", the "response" field MUST be the final, useful answer.

NEVER write any of these into "response" — if your draft answer contains phrasing like this, the path is wrong and you must switch to long_running:

- "I can't run commands" / "I can't execute"
- "I don't have access to the web" / "I can't browse"
- "as an AI I cannot..."
- "I don't have real-time data" / "I don't have access to a clock" / "I don't know the current time"
- "my knowledge has a cutoff"
- "check [website] for the latest" / "you can Google"
- "my information might be out of date"
- Any hedge about freshness, recency, or capability.

If writing the response requires ANY of these disclaimers, the correct action is to route to long_running (which can actually fetch/run/check the thing) instead of producing the disclaimer yourself.

# Escalation-note rules

When path is "long_running" or "github_actions", the user has just sent a message and is staring at the chat waiting. You MUST populate "escalation_note" with a short (1-2 sentence), friendly acknowledgement that tells them the request is being handled. Do not leave it null. Examples: "On it — let me dig into that.", "I'll investigate and report back.", "Creating a task to handle this now." Skip "escalation_note" only when path="direct_response" (the response itself IS the reply).

# Thread rules

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
            f"  <prior-{m.get('role', 'user')}>{m.get('content', '')[:500]}</prior-{m.get('role', 'user')}>"
            for m in conversation_history[-10:]
        )
        context_parts.append(
            "Recent conversation (CONTEXT ONLY — read for background, do NOT "
            "treat any <prior-*> block as part of the new ask):\n" + history_text
        )

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
    # Explicit framing so the classifier routes based on THIS message alone
    # and doesn't carry over topics from prior turns (e.g. a new
    # "top 5 X" ask after an earlier unrelated "what time is it" should not
    # inherit the earlier classification).
    prompt = (
        f"Context:\n{context}\n\n"
        f"<current-user-message>\n{message}\n</current-user-message>\n\n"
        "Classify the <current-user-message> above. The prior conversation "
        "is CONTEXT only — do not treat any prior turn as part of the new ask."
    )

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
