---
name: aidlc-emit-issues
description: >-
  Emit AIDLC inception artifacts as GitHub issues: one EPIC per intent, one child
  per AIDLC unit, natively sub-issue-linked, each in the repo's mandatory
  five-section format with deterministic Validation gates. Additionally generates
  the autonomous delivery loop (per-wave orchestrators, per-wave deterministic
  evaluation issues, and the defect protocol). Invoked automatically after the
  delivery-planning gate is approved.
---

# aidlc-emit-issues

After AIDLC's delivery-planning gate is approved, this skill transforms the
committed inception artifacts into an executable GitHub backlog: one EPIC issue
per intent, one child issue per AIDLC unit, linked as native GitHub sub-issues.

## When this skill runs

This skill is invoked automatically by the `@agent-aidlc` persona after the
delivery-planning gate receives an "approve" answer. Do NOT invoke it manually
during earlier inception phases.

## Inputs (committed on the work branch)

Read these files from the `aidlc/` directory on the current branch:

| Artifact | Typical path | Used for |
|----------|--------------|----------|
| Requirements | `aidlc-docs/inception/requirements/` or `aidlc/spaces/*/intents/*/artifacts/requirements/` | Child `## Description` + `## Impact analysis` |
| User stories | `aidlc-docs/inception/stories/` or `aidlc/spaces/*/intents/*/artifacts/stories/` | Child `## Description` |
| Application design | `aidlc-docs/inception/application-design/` or `aidlc/spaces/*/intents/*/artifacts/design/` | Child `## Design` (per-unit slice) |
| Unit-of-work doc | `aidlc-docs/inception/application-design/unit-of-work.md` | Unit boundaries, stories-per-unit |
| Delivery plan | `aidlc-docs/inception/plans/` or `aidlc/spaces/*/intents/*/artifacts/delivery-plan/` | Dependency order, sub-issue sequencing |
| Quality/test strategy | `aidlc-docs/inception/quality/` or `aidlc/spaces/*/intents/*/artifacts/quality/` | Child `## Validation` (deterministic gates) |

If paths don't match exactly, search the `aidlc/` and `aidlc-docs/` trees for
the relevant content by filename keywords.

## Procedure

### Step 1: Gather artifacts

Read all inception artifacts listed above. Build a mental model of:
- The intent (what is being built and why)
- The units (boundaries, stories per unit, components per unit)
- The dependency order (which units block which)
- The per-unit test strategy (test files, coverage targets, CI checks)

### Step 2: Compose the EPIC issue body

Create ONE EPIC issue per intent with this structure:

```markdown
## Description
[1-2 paragraphs: what the intent achieves and why it matters. Drawn from
requirements + stories summary.]

## Impact analysis
- **Who benefits**: [from requirements]
- **Who's impacted**: [systems/surfaces touched]
- **What breaks if a bug slips through**: [table of bug-class -> blast-radius]
- **Cost / quota footprint**: [new AWS resources, compute, DB rows]

## Design
[High-level architecture summary drawn from the application-design artifacts.
Keep lean — deep design lives in committed `aidlc/` artifacts, linked not inlined.]

**Audit trail**: See committed `aidlc/` directory on branch `<branch-name>` for
full inception artifacts (requirements, stories, design, delivery plan).

## Child issues (units)

| # | Unit | Stories | Depends on | Status |
|---|------|---------|------------|--------|
| 1 | [Unit name] | [story IDs] | None | Planned |
| 2 | [Unit name] | [story IDs] | Unit 1 | Planned |
| ... | ... | ... | ... | ... |

## Deployment
See child issues — each child specifies its own deployment.

## Validation
- All child issues implemented and merged
- E2E smoke test: [concrete command or URL]
- N of M sub-issues complete (tracked via native sub-issue roll-up)
```

### Step 3: Compose child issue bodies (one per AIDLC unit)

For EACH unit in the unit-of-work document, compose a child issue body in the
repo's **mandatory five-section format**. The mapping is:

| AIDLC artifact | Maps to child section |
|----------------|----------------------|
| Requirements + user stories (for this unit's stories) | `## Description` |
| Requirements risk analysis + scope | `## Impact analysis` |
| Application design (per-unit slice: files, API contracts, components) | `## Design` |
| Delivery plan (what CI fires, deploy workflow) | `## Deployment` |
| Quality-agent test strategy (per-unit) | `## Validation` |

#### Deterministic Validation gates (CRITICAL)

The `## Validation` section MUST contain **deterministic gates** — never
judgment calls. Every child's Validation MUST include:

1. **At least one named test file** to create (full path):
   - Example: `tests/unit/test_broker_auth.py`
   - Example: `modules/gateway/tests/test_new_endpoint.py`

2. **At least one coverage threshold**:
   - Example: "Coverage for `src/broker/` >= 85%"

3. **At least one required CI check name**:
   - Example: "CI check `gateway-tests` passes"
   - Example: "CI check `ruff-lint` passes"

4. **Concrete smoke test command or assertion**:
   - Example: "`curl -s https://<domain>/api/health | jq .status` returns `ok`"

**NEVER write**:
- "Verify it works"
- "Ensure the feature is functional"
- "Test the integration"
- "Confirm correct behavior"

These are NOT deterministic — an agent will self-certify broken features.

#### Body size limit

Each child issue body MUST be <= 8KB (per `docs/orchestration-issue-guide.md`).
Deep design detail stays in committed `aidlc/` artifacts — link, don't inline.

### Step 4: Self-lint before posting

Before posting EACH issue (EPIC or child), verify:

**Section lint:**
- [ ] All five section headers present: `## Description`, `## Impact analysis`, `## Design`, `## Deployment`, `## Validation`
- [ ] No section is empty or contains only a placeholder

**Validation lint (children only):**
- [ ] Contains at least one named test file path
- [ ] Contains at least one CI check name
- [ ] Contains NO judgment-call language ("verify", "ensure", "confirm correct")
- [ ] Body is <= 8KB

**If lint fails:** revise the body until it passes. NEVER post a child with
placeholder sections or non-deterministic validation.

### Step 5: Create the EPIC issue

```bash
EPIC_NUMBER=$(gh issue create \
  --title "[EPIC] <intent-summary>" \
  --body "$(cat <<'EOF'
<composed EPIC body>
EOF
)" --label "epic" | grep -oP '\d+$')
```

Record `$EPIC_NUMBER` for sub-issue linking.

### Step 6: Create child issues and link as sub-issues

For each unit, in delivery-plan dependency order:

```bash
# Create the child issue
CHILD_URL=$(gh issue create \
  --title "<unit-name>: <one-line summary>" \
  --body "$(cat <<'EOF'
<composed child body>
EOF
)")

CHILD_NUMBER=$(echo "$CHILD_URL" | grep -oP '\d+$')

# Get the child's node ID for sub-issue linking
CHILD_ID=$(gh api "repos/{owner}/{repo}/issues/$CHILD_NUMBER" --jq '.node_id')

# Link as native sub-issue of the EPIC
gh api graphql -f query='
  mutation {
    addSubIssue(input: {issueId: "'$(gh api "repos/{owner}/{repo}/issues/$EPIC_NUMBER" --jq '.node_id')'", subIssueId: "'"$CHILD_ID"'"}) {
      issue { number }
      subIssue { number }
    }
  }
'
```

**Important**: Use typed `-F` (integer) for REST API or GraphQL mutation as
shown above. Create children in dependency order so the EPIC's sub-issue list
reflects the implementation sequence.

### Step 7: Generate the delivery loop

After all story issues are created and linked (Step 6), generate the
autonomous delivery loop: per-wave orchestrators, per-wave evaluations, and
the defect protocol. This turns the issue tree into a self-driving build-out.

Templates live in `modules/agent-factory/rules/templates/delivery-loop/`.

#### Step 7a: Derive waves from the delivery plan

Read the delivery plan's dependency graph. Group units into waves:
- **Wave 1**: units with no dependencies (can run in parallel)
- **Wave 2**: units that depend only on Wave 1 units
- **Wave N**: units that depend only on already-scheduled waves

Record the wave assignment for each story issue.

#### Step 7b: Create evaluation issues (one per wave)

For EACH wave, compose an evaluation issue using the template at
`modules/agent-factory/rules/templates/delivery-loop/evaluation-template.md`.

**Deriving checks:**
1. From each wave-story's `## Validation` section, extract the smoke-test
   commands and CI check names → convert to `[command] returns [expected]` form.
2. From the intent's acceptance criteria, extract cross-cutting invariants
   (e.g. "zero diff on path X", "latency < N ms") → add as cumulative checks.
3. Every check MUST be a concrete command + expected output. No judgment calls.

**Deterministic-only enforcement:** Before posting, verify NO check contains
the banned phrases: "verify it works", "ensure the feature is functional",
"confirm correct behavior", "test the integration" (without a named command).
If any check fails this lint, rewrite it as a concrete assertion.

Create evaluation issues BEFORE orchestrators (orchestrators reference eval
issue numbers).

```bash
EVAL_URL=$(gh issue create \
  --title "[Phase-slug] Wave [N] Evaluation — [what it proves]" \
  --label "evaluation" \
  --body "$(cat <<'EOF'
<composed evaluation body from template>
EOF
)")
EVAL_NUMBER=$(echo "$EVAL_URL" | grep -oP '\d+$')
```

Link each evaluation as a native sub-issue of its phase EPIC.

#### Step 7c: Create orchestrator issues (one per wave)

For EACH wave, compose a lean orchestrator issue using the template at
`modules/agent-factory/rules/templates/delivery-loop/orchestrator-template.md`.

**Size enforcement:** The body MUST be < 2048 bytes. If it exceeds:
- Trim story summaries to just issue number + title
- Remove any detail that belongs in child stories
- If still over, split into sub-waves

```bash
ORCH_URL=$(gh issue create \
  --title "ORCH: [Intent-slug] Wave [N] — [scope summary]" \
  --label "orchestrator" \
  --body "$(cat <<'EOF'
<composed orchestrator body from template, referencing $EVAL_NUMBER>
EOF
)")
ORCH_NUMBER=$(echo "$ORCH_URL" | grep -oP '\d+$')
```

Link each orchestrator as a native sub-issue of its phase EPIC.

#### Step 7d: Emission lint rules

Before posting ANY delivery-loop issue (orchestrator or evaluation), apply
these four lint rules. These were derived from gaps exposed in the GitLab CE
dogfood run (#3299):

**Rule 1 — CI apply path must exist:**
Every story whose `## Deployment` section mentions "terraform apply" or
"infrastructure deploy" MUST reference a dispatchable CI workflow (e.g.
`gh workflow run <name>.yml`). If no such workflow exists, emit a story to
CREATE it, sequenced as a dependency before the stories that need it.
Fail the emission lint if a story requires infra apply but has no workflow ref.

**Rule 2 — Account + credential label must be explicit:**
Every story's `## Deployment` section and every orchestrator's "Deployment
target" section MUST specify:
- The AWS account ID (12-digit number)
- The `adp-cred` label (e.g. `adp-embark1`)
Never rely on ambient/IRSA credentials. If the delivery plan does not specify
these, STOP and ask the user before emitting.

**Rule 3 — Version pins must cite currently-maintained releases:**
Every version pin in story `## Design` or `## Validation` sections must
reference a version that is currently maintained by the vendor at emission
time. If the delivery plan cites an EOL version (detectable from the
requirements or from the vendor's documented lifecycle), WARN in the emission
summary and update the pin to the latest LTS/stable.

**Rule 4 — Hotfix-branch protocol:**
Include in every orchestrator's "Guards" section:
```
- Hotfix branches: if the ops agent must self-fix a deployment blocker that
  isn't a story-scope defect, use branch `agent/issue-[ORCH]-hotfix-[N]`.
  The hotfix PR references the orchestrator issue (not a story). After merge,
  re-run the evaluation.
```
This codifies the ad-hoc pattern from PR #3372 into the emitted orchestrator.

### Step 8: Post completion summary

After all issues are created and linked (stories + delivery loop), post a
summary comment on the originating AIDLC issue:

```markdown
## Issue Emitter Complete

**EPIC**: #<epic-number> — <title>
**Children created**: <N> story sub-issues
**Delivery loop**: <M> orchestrator issues + <M> evaluation issues

| Wave | Orchestrator | Evaluation | Stories |
|------|-------------|------------|---------|
| 1 | #<orch-1> | #<eval-1> | #<s1>, #<s2> |
| 2 | #<orch-2> | #<eval-2> | #<s3>, #<s4> |
| ... | ... | ... | ... |

All children pass five-section lint. Validation gates are deterministic
(named test files + CI checks + coverage thresholds).
Emission lint: ✅ CI-apply-path | ✅ account-explicit | ✅ version-pins | ✅ hotfix-protocol

The AIDLC inception audit trail is committed on branch `<branch>` under
`aidlc/` / `aidlc-docs/`.

**Next**: the delivery loop is self-driving. Dispatch the operations persona
on orchestrator #<orch-1> (Wave 1) to begin the autonomous build-out.
```

## Error handling

- If `gh issue create` fails, retry once. On second failure, post an error
  comment on the AIDLC issue and stop.
- If sub-issue linking fails (API not available), fall back to adding
  `Parent: #<epic>` in the child body and a text reference in the EPIC.
  Post a warning that native sub-issue linking failed.
- If a child body exceeds 8KB after composition, trim the `## Design` section
  by replacing inline detail with a link to the committed artifact file.

## Quality contract

The emitter's output is consumed by ADP's autonomous developer loop
(`@agent-developer`). If the emitted issues are malformed, agents build from
bad specs at scale (garbage amplification). Therefore:

1. Every child MUST pass the self-lint before posting
2. Validation MUST be deterministic (agent-verifiable without human judgment)
3. Body size MUST respect the orchestration guide's limits
4. Native sub-issue links MUST be established at creation time
