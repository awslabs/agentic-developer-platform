---
name: codex-bridge
description: >-
  Delegate a single bounded coding or code-review task to the OpenAI Codex CLI
  (runs against Bedrock), then review Codex's output yourself before committing.
  ONLY use this skill when the triggering issue or comment EXPLICITLY names Codex
  — e.g. "use Codex", "ask Codex to review this", "pass this to Codex", "have
  Codex write X". Never invoke it autonomously, speculatively, or because a task
  merely looks like something Codex could do. If nobody asked for Codex, do the
  work yourself and do not run this skill.
---

# codex-bridge

Hand a **single, bounded** task to the Codex CLI and bring the result back for
your review. Codex runs headless in this pod against Bedrock (`openai.gpt-5.5`);
auth is the pod's IRSA credential chain — there are no secrets to manage.

You (the supervising Claude agent) stay in the loop: Codex's output is a
**proposal**, not a commit. You read its diff/output, decide whether it's
correct, and only then integrate it.

## When to use this skill

Use it **only** when the person who triggered this run explicitly asked for
Codex. Concrete triggers (in the issue body or the comment that summoned you):

- "use Codex to …"
- "ask Codex to review this diff / this file / this PR"
- "pass this to Codex" / "have Codex write / implement …"
- "get a second opinion from Codex"

If you scan the triggering text and find **no** explicit mention of Codex,
**stop** — do not run this skill. Complete the task with your own tools. This
gate is a hard requirement: firing autonomously spends Bedrock budget on a model
the user did not choose and produces code authored by an unintended model.

## How to use it

### 1. Confirm the explicit request

Re-read the triggering issue/comment. Confirm it names Codex and identifies a
concrete, bounded task (write X, review file Y). If the ask is vague, ask the
requester to narrow it rather than guessing.

### 2. Run the wrapper

The wrapper is `scripts/run-codex.sh`. It closes stdin, enforces a hard timeout,
resets AWS credentials to the pod's IRSA defaults, and passes your instruction to
Codex as a single argument (no shell interpretation of the text).

**Write mode** — Codex authors/edits code in the current working directory:

```bash
.claude/skills/codex-bridge/scripts/run-codex.sh write "Add a hello_world() function to hello.py that prints 'hello world'."
```

**Review mode** — Codex reviews a file and reports findings (read-only intent):

```bash
.claude/skills/codex-bridge/scripts/run-codex.sh review path/to/changed_file.py
```

The script prints Codex's final message to stdout. A non-zero exit means Codex
failed or timed out — its stderr is surfaced; report it, do not silently retry
in a loop.

### 3. Review before committing

Treat the result as a proposal:

- **Write mode:** run `git diff` and read every change. Verify it does what was
  asked, matches the repo's conventions, and introduces nothing unexpected. Run
  the relevant tests/linters. Only commit if it passes your review; otherwise
  fix or discard it. You own the final diff.
- **Review mode:** treat Codex's findings as input to your own judgment — a
  cross-model second opinion. Decide which points are valid before acting.

### 4. Restate the run summary (so it lands on the live run page)

The wrapper prints a compact, per-step summary of what Codex did — one line per
reasoning step / command / file edit, Codex's final message, and a trailer with
the **session id**, **token usage**, and the raw JSONL log path. That output
sits inside the Bash tool result, so it does **not** reach the live run page on
its own.

**In your next message after the wrapper returns**, restate that summary so it
reaches operators watching the run (it lands one turn late) and appears in the
completion comment. Keep it compact — **≤ ~20 lines**:

- the per-step lines Codex emitted (reasoning / `exec` / `edit`), trimmed;
- Codex's final message (or a short paraphrase if it's long);
- the **session id** and **token usage** from the trailer.

Do **not** paste the raw JSONL — it stays on disk. Reference its path (shown in
the trailer, under `/tmp/codex-runs/`) only if you need to point at it for deep
debugging.

## Guardrails

- **The explicit-trigger gate is mandatory.** No Codex mention → do not run this
  skill.
- **One bounded task per invocation.** Don't script open-ended loops of Codex
  calls; each run is supervised.
- **You review the output.** Codex authors a proposal; you are accountable for
  what gets committed.
- **Never pass secrets in the instruction.** The instruction text is the task
  description, not a place for credentials.
