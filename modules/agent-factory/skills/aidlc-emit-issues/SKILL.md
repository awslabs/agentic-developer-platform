---
name: aidlc-emit-issues
description: >-
  Emit AIDLC inception artifacts as GitHub issues: one EPIC per intent, one child
  per AIDLC unit, natively sub-issue-linked, each in the repo's mandatory
  five-section format with deterministic Validation gates. Invoked automatically
  after the delivery-planning gate is approved.
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

### Step 7: Post completion summary

After all issues are created and linked, post a summary comment on the
originating AIDLC issue:

```markdown
## Issue Emitter Complete

**EPIC**: #<epic-number> — <title>
**Children created**: <N> sub-issues

| # | Issue | Unit | Depends on |
|---|-------|------|------------|
| 1 | #<number> | <unit-name> | None |
| 2 | #<number> | <unit-name> | #<dep> |
| ... | ... | ... | ... |

All children pass five-section lint. Validation gates are deterministic
(named test files + CI checks + coverage thresholds).

The AIDLC inception audit trail is committed on branch `<branch>` under
`aidlc/` / `aidlc-docs/`.

**Next**: dispatch `@agent-developer` on child #1 (no dependencies) to begin
implementation.
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
