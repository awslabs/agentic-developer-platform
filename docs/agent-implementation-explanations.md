# Agent implementation explanations

The first reporting increment asks agents to teach the implementation while they
work. A reader should understand the relevant components, why changes were made,
what the evidence establishes, and how to reproduce or continue the work.

## When explanations appear

The shared [communication policy](../modules/agent-factory/rules/personas/shared/human-communication.md)
asks for normal assistant-text explanations after initial investigation and
before editing, before consequential changes, after discoveries or checks, at
published checkpoints, and at completion/handoff. The final response should
contain a self-contained implementation walkthrough with relevant reproduction
steps and evidence limits. Small questions and routine actions need less detail.

For example, before a streaming implementation step:

> The gateway currently waits for the response to finish. To deliver live
> updates, it needs to forward chunks as they arrive. I am changing that response
> path, then checking whether the first event reaches the client before the
> response ends. That check alone will not establish reconnect recovery.

This example describes the expected explanation style; it is not a claim that
dashboard streaming has been implemented.

The prompt sets reporting expectations. It does not enforce milestone timing or
prove that an explanation is correct. There is no new model call per tool or
new periodic reminder in this increment. Existing checkpoint reminders continue
to operate independently.

## Existing GitHub views

- The issue's existing live status comment shows the latest assistant explanation
  above lifecycle stages and technical activity. It retains the explanation's
  timestamp when heartbeats refresh the page. Empty/duplicate explanation updates
  do not replace it. Its excerpt is limited to 16 KiB with a visible notice.
- The Check Run shows assistant explanations in order, with technical tool
  previews below. It no longer invents a fixed plan from the first text fragment
  or clips every explanation to 500 characters. When the document exceeds the
  60 KiB display budget, it prioritizes the latest explanation and recent tools,
  and labels omitted/shortened content. Existing throttling and the PATCH
  circuit-breaker are unchanged.
- The normal final response remains the issue outcome. Failure summaries retain
  the last explanation; a normal completion uses the agent's final report.

These paths publish intentional assistant text. Private thinking blocks and
tool-result payloads are excluded from explanation extraction. The policy asks
the agent to avoid credentials and unsupported claims; this increment adds no
general-purpose secret detector or evidence-verification engine.

## Transcript storage

The Node worker produces two separate files on result/cleanup, using a temporary
file and rename so a partial write is not published as a complete transcript:

| File | Consumer and content |
|---|---|
| `/tmp/adp-check-run-final.md` | GitHub finalization; bounded display |
| `/tmp/adp-run-transcript.md` | S3 archive; captured assistant explanations in chronological order, followed by tool previews and recent Codex activity |

The archive preserves all captured assistant text blocks beyond the old
500-character and GitHub document limits. Markdown headings and fenced code
work in the existing transcript viewer without GitHub-only disclosure markup.
It is a record of agent-authored explanations, not an independently verified
account of results. Tool calls are attempts; their previews are not result logs.
Only the most recent 200 compact Codex activity lines are retained here; delegated
raw event history remains a separate artifact.

The entrypoint uploads the independent transcript using the existing S3 key and
invocation-link mechanism. If it is unavailable, it can preserve the GitHub
display with an explicit potentially incomplete fallback notice. A missing
GitHub display does not prevent upload of an available transcript. Upload and
file failures remain non-fatal, and missing artifacts are logged.

This mechanism still depends on the existing hosted-worker reporting setup and
configured archive bucket. It does not recover discarded historical content,
capture full terminal output, or guarantee archival after a hard pod loss before
cleanup. Dashboard SSE and the structured reporting design remain separate work.

## Verification and review

The Agent Reporting Tests workflow exercises text extraction, multi-block and
oversized explanations, Unicode byte limits, ordered archives, failed writes,
GitHub circuit-breaker behavior, labeled legacy fallback, S3 payload preservation,
and shared policy loading/staging for worker, chat, gateway and delegated runs.
Tests use mocked GitHub/S3 calls and synthetic messages; they do not launch agents
or establish deployed behavior.

After rollout, review a representative real run with a teammate unfamiliar with
the implementation. Ask them to explain the component flow and a decision, find
the code, reproduce a meaningful check and diagnose a representative failure
using the transcript. Record gaps and refine the instructions. Automated format
tests alone do not establish knowledge transfer.
