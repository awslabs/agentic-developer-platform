# Port notes — `superplane` skill

Records what was carried over from the upstream Superplane agent skill, what was
deliberately changed, and what was left behind. Part of EPIC #4910 unit U4 (R9).

**Upstream source:**
`modules/domain-apps/ai-super-plane/reference/src/superplane-skill/`
(9 files: `SKILL.md`, `README.md`, `install.sh`, and six `references/*.md`).

## File-by-file disposition

| Upstream file | Disposition | Why |
|---|---|---|
| `SKILL.md` | **Ported**, with the auth section rewritten and a read-only/spending split added | The operational content is the value; the auth model is the one thing ADP replaces |
| `references/api-reference.md` | Not copied; referenced in place | Documents the upstream CLI surface verbatim, including its token-in-config auth. Staging it into the image would put instructions contradicting the ported skill's identity rules in front of the agent. |
| `references/workspace-management.md` | Not copied; referenced in place | As above |
| `references/model-deployment.md` | Not copied; referenced in place | As above |
| `references/node-management.md` | Not copied; referenced in place | As above |
| `references/troubleshooting.md` | Not copied; referenced in place | As above; its auth-error remedies are `superplane login`, which an ADP agent must not run |
| `references/examples.md` | Not copied; referenced in place | As above |
| `README.md` | **Dropped** | Human-facing install instructions for a laptop IDE (`pip install superplane-cli`, `superplane login`). Superseded by the worker-image build. |
| `install.sh` | **Dropped** | Copied the skill into `~/.claude/skills/` and `~/.kiro/skills/` on a developer machine. On ADP, `stage-personas.sh` stages skills into the image at build time — the installer's whole job. Keeping it would ship a script that writes into a home directory at run time, which is both redundant and a way for a run to mutate its own instruction set. |

### On not copying the six reference docs

This is the one disposition worth challenging, so the reasoning is explicit.

The docs contain genuinely useful CLI flag detail. But a skill directory is staged
wholesale into `/app/skills/superplane/` and everything in it is agent-readable
instruction. Three of the six tell the agent to run `superplane login` or to read
a token out of `~/.superplane/config.yaml` as the remedy for an auth failure —
precisely the two behaviours `SKILL.md` now forbids. Staging both means the agent
reads contradictory instructions and the outcome depends on which it reads last.

Rewriting all six to strip their auth guidance was the alternative. It was
rejected for this unit because it is ~2,000 lines of upstream CLI documentation
that no ADP-side test can validate — the CLI is not in the worker image yet, so a
rewritten copy would be unverifiable prose that drifts from the real CLI silently.
Pointing at them in the reference tree keeps them available to a human, and keeps
the single authoritative auth instruction in `SKILL.md`.

If the Superplane CLI is added to the worker image, revisit this: at that point
the flag detail becomes load-bearing for the agent and porting the docs (with auth
sections replaced, not merely deleted) is worth the effort.

## Substantive changes from upstream

### 1. Authentication (the load-bearing change)

Upstream bootstrap step: run `superplane login`, which writes a JWT to
`~/.superplane/config.yaml`; `SUPERPLANE_API_KEY` overrides it.

On ADP:

- The agent runs under ADP identity. It holds no long-lived provider token.
- An interactive login cannot complete in a worker pod regardless.
- The skill explicitly forbids reading, printing or echoing the config file or any
  credential environment variable — including "just to check whether it is set".
  A credential in agent output lands in the run transcript and the log sink, so
  the disclosure is already in three places before anyone reviews it.
- An auth failure is a **stop-and-report** condition, not something the agent
  tries to resolve. Upstream told the agent to fix it by logging in; on ADP that
  instruction would send the agent looking for a credential, which is the
  behaviour we most want to prevent.
- The skill also forbids asking the user to paste a token into an issue comment
  (permanent, world-readable on a public repo) and points at
  `/settings/credentials` instead.

### 2. Read-only vs. spending is made explicit

Upstream listed all commands in one flat set of tables. The port sorts every
command into read-only or spends/mutates, and requires a cost estimate plus user
confirmation before anything in the second column.

This mirrors R9 acceptance criterion 3 as enforced in the MCP tool surface
(`../../../tools/superplane-mcp/`), where read-only capacity discovery is a
distinct tool from allocation. Same invariant, expressed in the medium each
surface has: a capability check in the tool surface, an explicit instruction in
the skill. The skill cannot *enforce* it — it is a prompt — which is why the
enforcement lives in the tool surface and the skill only makes the split legible.

### 3. CLI installation

Upstream: if the CLI is missing, tell the user to `pip install superplane-cli`.

Port: if the CLI is missing, **stop and report an image gap**. A package installed
at run time is invisible to the next run, unpinned, and unreviewed. The CLI
belongs in the worker image Dockerfile.

Note the consequence: **this skill is currently non-functional in the worker
image**, because the Superplane CLI is not installed there. That is deliberate and
in scope for U4 — the unit ports the skill onto the ADP runtime; adding the CLI to
the image is a separate change with its own review (base-image size, pinning,
supply chain). Until then the skill's bootstrap step correctly reports the gap
instead of failing confusingly deeper in a workflow.

### 4. Frontmatter

Upstream had `name` and `description` only. The port adds `allowed-tools`
(`Bash Read Write`) and a `metadata` block recording the port provenance and the
authentication model, matching the shape used by the cyber domain pack's skills.

## Unverified

The upstream CLI surface documented here is transcribed from the upstream skill,
not exercised: the Superplane CLI is not present in the ADP worker image, so no
command in this skill has been run end to end from ADP. Flag names and outputs are
as upstream documented them. When the CLI lands in the image, the command tables
should be verified against `superplane --help` and corrected.
