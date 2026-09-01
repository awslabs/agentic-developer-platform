---
name: aidlc-emit-issues
description: >-
  Emit AIDLC inception artifacts as GitHub issues: one EPIC per intent, one child
  per AIDLC unit, natively sub-issue-linked, each in the repo's mandatory
  five-section format with deterministic Validation gates. Delivery loop
  generation is a two-stage protocol: Step 7 composes loop drafts as branch
  artifacts (invoked in Run A after delivery-planning approval); Step 8
  materializes the issues from approved drafts (invoked in Run B after
  loop-proposal approval). Orchestrators, evaluations, and defect protocol are
  created only after human review of the proposed wave structure.
---

# aidlc-emit-issues

After AIDLC's delivery-planning gate is approved, this skill transforms the
committed inception artifacts into an executable GitHub backlog: one EPIC issue
per intent, one child issue per AIDLC unit, linked as native GitHub sub-issues.
Delivery loop generation is split across two runs via the `loop-proposal` gate:
Run A composes loop drafts as branch artifacts for review; Run B materializes
the issues from approved drafts.

## When this skill runs

This skill is invoked by the `@agent-aidlc` persona in two contexts:
- **Run A** (delivery-planning gate approved): Steps 1–7 — create EPIC + story
  children, compose delivery-loop drafts, commit, gate.
- **Run B** (loop-proposal gate approved): Step 8 — materialize loop issues
  from approved drafts, Step 9 — post completion summary.

Do NOT invoke it manually during earlier inception phases.

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

#### Fixture provenance rule (cross-boundary stories)

When a story adds MSW mocks, test fixtures, or mock data for a backend
endpoint, its `## Validation` section MUST include:

> Fixtures must be derived from the backend schema file (`<path>`) or a
> captured real response — not written from the frontend type.

Name the specific backend schema/model file. This prevents the
"closed-loop self-consistent wrongness" failure mode where frontend types
invent fields that the backend never sends, and both mocks and tests
validate the invented shape (ref: #3675, #2415).

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

**Stories are created INERT — no dispatch triggers of any kind:**
- NEVER apply agent-trigger labels (`agent-developer`, `agent-operations`,
  `agent-reviewer`, or any `agent-*` persona label) to a story issue
- NEVER post an `@agent-<persona>` mention in a story's body or comments
- NEVER include an `@agent-<persona>` mention in the composed child body

Dispatching stories is the ORCHESTRATOR's job, at wave-execution time, in
dependency order (see the orchestrator template's "How to run" section). A
story dispatched at creation time bypasses wave sequencing entirely — every
wave implements at once, before the loop-proposal gate has even been reviewed
(this happened on EPIC #3557 and spawned 12 duplicate/noise PRs, bug #3626).

### Step 7: Compose delivery-loop drafts (no issue creation)

After all story issues are created and linked (Step 6), compose the delivery
loop as reviewable branch artifacts. **Do NOT create any issues in this step.**
The drafts are committed for human review; materialization happens in Step 8
after the `loop-proposal` gate is approved.

Templates live in `modules/agent-factory/rules/templates/delivery-loop/`.

Output directory: `aidlc/spaces/issue-<N>/construction/loop-proposal/`

#### Step 7a: Derive waves from the delivery plan

Read the delivery plan's dependency graph. Group units into waves:
- **Wave 1**: units with no dependencies (can run in parallel)
- **Wave 2**: units that depend only on Wave 1 units
- **Wave N**: units that depend only on already-scheduled waves

Record the wave assignment in a `wave-map.md` file:

```markdown
# Wave Map

| Wave | Story Issues | Depends on |
|------|-------------|------------|
| 1 | #<s1>, #<s2> | — |
| 2 | #<s3>, #<s4> | Wave 1 |
| ... | ... | ... |

**Account ID**: <12-digit>
**Credential label**: <adp-cred label>
**EPIC**: #<epic-number>
```

Commit to: `aidlc/spaces/issue-<N>/construction/loop-proposal/wave-map.md`

#### Step 7b: Compose evaluation issue drafts (one per wave)

For EACH wave, compose an evaluation issue body using the template at
`modules/agent-factory/rules/templates/delivery-loop/evaluation-template.md`.

**Deriving checks:**
1. From each wave-story's `## Validation` section, extract the smoke-test
   commands and CI check names → convert to `[command] returns [expected]` form.
2. From the intent's acceptance criteria, extract cross-cutting invariants
   (e.g. "zero diff on path X", "latency < N ms") → add as cumulative checks.
3. **Live API-contract check (Rule 5):** if the wave is cross-boundary
   (frontend consumes a typed backend response), emit the contract check per
   Step 7d Rule 5. Read the TS interface from the story's Design section,
   enumerate expected fields, and emit a curl + jq assertion comparing live
   response keys to the frontend type's field list. This check validates
   against the REAL deployed endpoint, not the story's own mocks/fixtures.
4. Every check MUST be a concrete command + expected output. No judgment calls.

**Deterministic-only enforcement:** Verify NO check contains the banned
phrases: "verify it works", "ensure the feature is functional", "confirm
correct behavior", "test the integration" (without a named command). If any
check fails this lint, rewrite it as a concrete assertion.

Commit each draft to:
`aidlc/spaces/issue-<N>/construction/loop-proposal/evaluation-wave-<K>.md`

Each file contains the full issue body (title on line 1 as `# [title]`,
body below). No `gh issue create` — these are review artifacts only.

#### Step 7c: Compose orchestrator issue drafts (one per wave)

For EACH wave, compose a lean orchestrator issue body using the template at
`modules/agent-factory/rules/templates/delivery-loop/orchestrator-template.md`.

**Size enforcement:** The body MUST be < 2048 bytes. If it exceeds:
- Trim story summaries to just issue number + title
- Remove any detail that belongs in child stories
- If still over, split into sub-waves

Commit each draft to:
`aidlc/spaces/issue-<N>/construction/loop-proposal/orchestrator-wave-<K>.md`

Each file contains the full issue body (title on line 1 as `# [title]`,
body below). No `gh issue create` — these are review artifacts only.

**Eval-number placeholder:** evaluation issues do not exist yet at draft time,
so the orchestrator draft's `## Evaluation` section MUST use the placeholder
`#[EVAL_WAVE_<K>]` (e.g. `run #[EVAL_WAVE_2]`). Step 8 substitutes the real
issue number after the wave's evaluation issue is created. This substitution
is the ONLY permitted difference between the gated draft and the created issue.

#### Step 7d: Emission lint rules

Before committing the drafts, apply these five lint rules to EVERY draft
(orchestrator and evaluation). Rules 1–4 were derived from gaps exposed in
the GitLab CE dogfood run (#3299); Rule 5 from the dashboard crash #3675:

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

**Rule 5 — Live API-contract check (cross-boundary waves):**
When a wave's stories introduce or consume a typed API response across the
frontend/backend boundary (i.e. a frontend component fetches from a backend
endpoint and maps the response into a TypeScript interface), the emitted
evaluation MUST include a deterministic contract check that:

1. Calls the REAL deployed endpoint (authed, via the same `adp-cred` +
   gateway-token path that evals already use for smoke checks).
2. Extracts the response's key set (e.g. `curl ... | jq '[.[] | keys] | flatten | unique'`
   for arrays, or `jq 'keys'` for objects).
3. Asserts every field referenced by the frontend TypeScript interface exists
   in the live response. The expected field list is enumerated at emission time
   by reading the TS interface from the story's `## Design` section.
4. Fails the wave if ANY expected key is missing or renamed.

**Triggering condition:** this rule applies when the wave contains stories
where a frontend component declares a TypeScript response type AND a backend
endpoint serves that response — detectable by: (a) the story's Design section
names both a TS interface and an API endpoint path, OR (b) stories in the
wave span both `modules/gateway/frontend/` and `modules/gateway/src/` (or
equivalent backend path). It does NOT key on labels — structural detection
only.

**Emitted check format (in the evaluation draft):**
```
N. Live API-contract: `curl -s -H "Authorization: Bearer $TOKEN" https://<domain>/api/<path> | jq '<extract-expr>'` contains keys [<field1>, <field2>, ...].
   Source: frontend type `<InterfaceName>` in `<file-path>`.
   Expected fields: <comma-separated list from the TS interface>.
   Rule: every field in the frontend type MUST exist in the live response keys.
```

**Fixture-provenance sub-check:** if the wave's stories add MSW mocks or
test fixtures for the same endpoint, add a second check:
```
N+1. Fixture provenance: `jq 'keys' <fixture-file>` ⊆ live response keys.
   Source: fixture derived from backend schema `<schema-file>`.
   Rule: fixture keys must be a subset of live-response keys (no invented fields).
```

**What this prevents:** the failure mode from #3675 where `RunStatsResponse`
invented `failed_at`, `today.succeeded`, `today.spend` that the backend
`StatsResponse` never sends — unit tests, MSW mocks, AND the eval all
validated against the invented shape because nothing compared to the live API.

**If any lint rule fails:** fix the draft and re-lint. Do not commit drafts
that fail lint. Report lint results in the gate comment.

#### Step 7e: Emit the machine-readable proposal (`proposal.json`)

The markdown drafts above are for the human reviewing the wave structure. This
step emits the same wave structure in the form the **orchestration engine** reads,
so that on approval the plan can be executed by machine instead of hand-tracked
(issue #4528).

Emit exactly one file:

`aidlc/spaces/issue-<N>/construction/loop-proposal/proposal.json`

Write the wave structure you just derived in Step 7a directly as JSON — do **not**
parse it back out of `wave-map.md`. The markdown is a rendering for humans; this
file is the machine form of the same decision, and both come from the wave
assignment you already hold.

**Shape** (the `LoopProposal` contract, defined in
`modules/gateway/src/orchestration/proposal.py` — that module is authoritative):

```json
{
  "flow_slug": "<kebab-case slug for this delivery loop, e.g. aidlc-delivery-loop-<N>>",
  "title": "<the intent's title>",
  "org_id": "",
  "spec_revision": "issue-<N>-r1",
  "intent_ref": "<N>",
  "nodes": [
    {"address": "<flow_slug>/epic-<EPIC>/wave-<K>/<node-ref>",
     "kind": "story", "title": "<story title>", "issue_ref": "<story issue number>"},
    {"address": "<flow_slug>/epic-<EPIC>/wave-<K>/eval",
     "kind": "eval", "title": "Wave <K> evaluation: <what it proves>"}
  ],
  "edges": [
    {"from_address": "<flow_slug>/epic-<EPIC>/wave-<K>/<node-ref>",
     "to_address": "<flow_slug>/epic-<EPIC>/wave-<K>/eval"}
  ]
}
```

**Rules — a document breaking any of these is refused by the engine:**

1. **`org_id` MUST be the empty string.** The worker overwrites it with the
   tenant resolved for the run. Never write a tenant id here, even a correct-looking
   one: the engine compares the declared org against its own resolved value and
   rejects a mismatch, and a guessed value turns a working registration into a 422.
2. **Addresses are exactly four segments**, `flow/epic/wave/node`, and the first
   segment MUST equal `flow_slug` on every node.
3. **`kind` is one of `story`, `eval`, `gate`.** Never `wave`, `epic` or `flow` —
   containers are derived by rolling up their members, not declared as nodes.
4. **Every wave containing stories contains exactly one `eval` node.** One story
   node per story issue created in Step 6, carrying its number in `issue_ref`.
5. **Edges encode the wave order** from Step 7a: each wave's stories point at that
   wave's eval, and each wave's eval points at the next wave's stories.
6. **Declare no `gate` nodes unless the delivery plan genuinely calls for one.**
   Registration inserts an acceptance gate in front of the whole plan, and (for a
   proposal that declares no gate of its own) a gate at every wave boundary. A
   hand-declared gate suppresses that default, so declare one only deliberately.
7. **The edge set MUST be acyclic**, and every endpoint MUST resolve to a declared
   node.

**Validate before committing** — the same rules the engine enforces, run locally:

```bash
python3 .github/scripts/validate_loop_proposal.py --authored \
  aidlc/spaces/issue-<N>/construction/loop-proposal/proposal.json
```

`--authored` is required here and says "the tenant is not resolved yet", which is
correct for the file you just wrote: rule 1 above mandates a blank `org_id`, and
without the flag the validator rejects the document on that field alone. **If you
see a complaint about `org_id`, add the flag — do not fill in a tenant id to make
the validator happy.** Inventing one is the single worst edit you can make to this
file: the engine compares the declared tenant against the one it resolved for the
run, so a guessed value converts a working registration into a hard 422.

Exit 0 means the document is acceptable. **A non-zero exit is a lint failure: fix
the document and re-run — do not commit a proposal that fails validation.** The
check is advisory (nothing forces you to run it), which is exactly why you must.

Commit this file alongside the Step 7a–7c drafts. Reference example (a valid
two-wave document): `aidlc/spaces/issue-4120/construction/loop-proposal/example-proposal.json`.

**What happens after you commit it:** the worker's finish path finds this file,
fills in `org_id`, and POSTs it to the gateway, which stores it as an **inert
draft** — visible in the graph UI, executing nothing. The run's closing comment
names the plan id and the one command a human types to make it live. If the file
is absent, registration is silently skipped and the loop stays markdown-only, so
omitting it is a silent regression rather than a visible error.

### Step 8: Materialize delivery loop (on loop-proposal approval)

This step runs ONLY after the `loop-proposal` gate is approved (Run B). It
creates the actual GitHub issues from the committed drafts.

#### Pre-materialization lint

Before creating any issues, re-lint ALL issue drafts (the `.md` files) in
`aidlc/spaces/issue-<N>/construction/loop-proposal/` against the Step 7d rules.
If any draft fails (e.g. a manual edit broke a rule since the gate was posted),
**REFUSE** — do not create issues. Re-gate with an error summary listing which
rules failed on which drafts.

`proposal.json` is not an issue draft and the Step 7d rules do not apply to it.
Re-validate it with its own checker instead, and refuse on a non-zero exit:

```bash
python3 .github/scripts/validate_loop_proposal.py --authored \
  aidlc/spaces/issue-<N>/construction/loop-proposal/proposal.json
```

`--authored` for the same reason as in Step 7e: `org_id` is still blank at this
point, because the worker fills it in at registration and nothing between here and
there writes it.

#### Idempotency guard

Before creating each issue, check whether an issue with the same title already
exists as a sub-issue of the EPIC. If it does, skip creation for that issue.
This prevents duplicates when Run B is re-triggered (e.g. after a transient
`gh` failure).

Each draft carries its own title on line 1 (`# [title]`). Derive the issue
title from that line and strip it from the body — the body posted to GitHub
must not repeat the title as a heading:

```bash
DRAFT="aidlc/spaces/issue-<N>/construction/loop-proposal/evaluation-wave-<K>.md"
TITLE=$(head -1 "$DRAFT" | sed 's/^# *//')
BODY_FILE=$(mktemp)
tail -n +2 "$DRAFT" | sed '/./,$!d' > "$BODY_FILE"

# Check for existing issue with this title before creating (any state)
EXISTING=$(gh issue list --search "in:title \"$TITLE\"" --state all \
  --repo {owner}/{repo} --json number --jq '.[0].number // empty')
if [ -n "$EXISTING" ]; then
  echo "Skipping: \"$TITLE\" already exists as #$EXISTING"
else
  EVAL_URL=$(gh issue create \
    --title "$TITLE" \
    --label "evaluation" \
    --body-file "$BODY_FILE")
  EVAL_NUMBER=$(echo "$EVAL_URL" | grep -oP '\d+$')
fi
```

#### Creation order (MANDATORY)

1. Create evaluation issues FIRST (orchestrators reference eval issue numbers)
2. Create orchestrator issues SECOND
3. Link ALL as native sub-issues of the EPIC
4. Kick off execution LAST: dispatch the **wave-1 ORCHESTRATOR issue** via
   `adp-trigger --persona operations --issue <WAVE_1_ORCH_NUMBER> --reason "kick off delivery loop"`.
   Do NOT post an `@agent-operations` comment — a bot-authored mention does not
   reliably dispatch and breaks correlation lineage. `adp-trigger` stamps lineage
   at ingress so the entire loop stays under the originating chain.

This ordering ensures orchestrators can reference eval issue numbers, and
dispatch only fires after all issues exist.

**The wave-1 orchestrator `adp-trigger` call is the emitter's ONLY dispatch action.**
The operations agent driving the orchestrator dispatches each story with its own
`adp-trigger` call, in dependency order (orchestrator template "How to run"). The
emitter NEVER labels, mentions, or otherwise triggers story issues — not at
creation (Step 6), not at kickoff (this step). Agent→agent dispatch is ALWAYS
`adp-trigger`, never an `@agent-<persona>` comment ([[feedback_agent_mention_parser_quirk]],
[[feedback_trigger_agents_by_mention_not_label]]).

Orchestrator drafts contain `#[EVAL_WAVE_<K>]` placeholders (Step 7c).
Substitute the real eval issue numbers before creating — this substitution is
the only permitted change to the gated draft body:

```bash
# After all evals exist, create orchestrators referencing them
DRAFT="aidlc/spaces/issue-<N>/construction/loop-proposal/orchestrator-wave-<K>.md"
TITLE=$(head -1 "$DRAFT" | sed 's/^# *//')
BODY_FILE=$(mktemp)
tail -n +2 "$DRAFT" | sed '/./,$!d' | sed "s/#\[EVAL_WAVE_<K>\]/#${EVAL_NUMBER}/g" > "$BODY_FILE"

ORCH_URL=$(gh issue create \
  --title "$TITLE" \
  --label "orchestrator" \
  --body-file "$BODY_FILE")
ORCH_NUMBER=$(echo "$ORCH_URL" | grep -oP '\d+$')
```

Apply the same idempotency guard (title search, any state) before each create.

Link each orchestrator and evaluation as a native sub-issue of the EPIC (same
GraphQL mutation as Step 6).

### Step 9: Post completion summary

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
Emission lint: ✅ CI-apply-path | ✅ account-explicit | ✅ version-pins | ✅ hotfix-protocol | ✅ api-contract-check

The AIDLC inception audit trail is committed on branch `<branch>` under
`aidlc/` / `aidlc-docs/`.

**Next**: the delivery loop is self-driving. The operations persona has been
dispatched on orchestrator #<orch-1> (Wave 1); it will dispatch stories in
dependency order and advance waves as evaluations close green.
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
