# UI, CLI and agent experience

**Everything under Product routes/commands below is proposed.** Existing login,
tenant/team management, connection and credential routes remain unchanged. The
[operation catalog](openapi.json) is common to all clients. Direct use does not
require an agent/model; optional guidance never becomes the only editing path.

## Current ADP UI placement and interactive mockup

Use the existing authenticated `MainLayout` outlet, Blueprint header/sidebar,
organization selector and shared Button/Card/form components. Add a single
**Products** navigation item alongside Dashboard; product-specific tabs stay inside
the selected Product. Preserve existing navigation and permission/feature gates.
The default UI uses a 232px `#17293d` sidebar, `#245ac4` primary, `#f4f7fb`
background and 6px card corners. Source inspection for the mockup is pinned at
`ac519dd2d4404d6dc87d9a0af28004c3debc187c`; this complements the older backend audit.
Do not introduce a separate application shell or treat feature-gated `/next` as
this default UI. Existing account-admission onboarding stays separate.

[Open the interactive HTML mockup](../mockups/product-onboarding.html) locally;
[placement and usage notes](../mockups/product-onboarding.md) list source links.
The entry journey is Products → add/select Product → onboarding → saved profile.
Repository, team and AWS-account flows reuse existing Connections, Organizations
and Credentials APIs. Product/Project fields are shared through the mapped Product
team; routine edits do not wait for owner approval. A greenfield draft can wait
for team selection without a separate privacy choice. Run visibility is separate.

The standalone mockup simulates separate saved state and local form drafts. It
uses the strict Profile/Session/Binding shapes, four persisted sections and
`profile-1` progress. Its Products list, Project switch, financial suggestions,
conflict resolution and scoped CLI resume follow the contracts below. Export is
a typed Profile JSON document. Browser-local fixtures are not a live API or an
installed CLI; preview-only failure controls sit outside the product workflow.

Keep the first save short: name, purpose, intended users. Default the authorized
owner and create the first Project automatically. Hide existing-Project selection
and domain under optional details. With one scope, hide the selector and offer
**More options → Manage delivery scopes**. With multiple scopes, show a
**Delivery scope** selector; this is the same canonical Project identity in routes,
CLI and exports. Additional Projects, Portfolios, cross-product Initiatives
and Releases appear only when useful; the [shared data model](data-model.md)
keeps their identities and lineage consistent without adding onboarding forms.

## Routes and screens

| Proposed route | Screen, data and actions |
|---|---|
| `/products` | Authorized Product cards, search within loaded/paginated results, Create. No cross-Project totals. |
| `/products/new` | One compact form: name, purpose/users, authorized owner default, starting point; Create Project default or select existing authorized Project. No repo/environment/budget required. |
| `/products/:productId/projects/:projectId` | Product workspace, current Profile summary, editable sections, source/provenance drawer, one/two hints; Context panel indicates real availability. |
| `/products/:productId/projects/:projectId/onboarding/:sessionId` | Four-section guided workspace with optional chat and always-available structured editing; persistent progress, handoff status, Resume in CLI. |
| `/products/:productId/projects/:projectId/history` | Authorized scoped change list and frozen revision inspection; current source/access overlay. No unfiltered aggregate snapshot export. |

Existing destinations (S07): `/settings/connections`, `/settings/credentials`,
`/settings/credentials/aws/connect`, `/admin/organizations`. Open scoped in-place
drawers when supported, otherwise return to the saved onboarding route. The old
`/onboarding/*` account-admission routes remain separate. A user without admin
rights sees Request administrator action, not an admin API called with escalated
credentials. Add Product navigation only when the server reports capability support.

Existing backend reuse (S18): team creation is
`POST /admin/organizations/{org_id}/departments/{dept_id}/teams` (org-admin
permission); existing-person placement is
`POST /admin/organizations/{org_id}/members` (platform admin); team assignment is
`POST /admin/organizations/{org_id}/teams/{team_id}/members`; pre-login GitHub
assignment is `POST /admin/identity/organizations/{org_id}/github-members`
(platform admin). Paths here are backend paths before the public `/api` prefix.
Use the returned organization-local `users.id`, not the incoming global person ID.
Discovery can suggest an already verified mapping; it never silently changes team
membership or enrolls a GitHub identity. Denied administrators use the existing
request/handoff experience rather than a Product-specific invitation endpoint.

### Create and workspace wireframes

```text
Products / New                        Workspace: Example team [change]
Name            [Example Planner                                  ]
Purpose         [Coordinate small releases                        ]
Intended users  [Maintainers                         + Add         ]
Owner           [Current member v]     Starting point [Idea v]
Optional details [>] Domain; create Project default / select existing
Owner defaults to current member. Team can be added after saving.
Creates identity and saves information. Creates no external resources.
[Save product]                                  [Cancel]
```

```text
Example Planner / Project A       Saved revision 7       [History]
100% (2/2 selected checks) · Information maturity: Saved, no repository
No execution authority granted. [Explain checks] [Resume in CLI]
-----------------------------------------------------------------
Product Information  [Edit] | Basic | Advanced | Financials
Purpose: Coordinate small releases     confirmed by owner
Repository: None selected              [Choose discovered source]
Context: unavailable — showing confirmed Profile facts only
Next: Connect a repository to enable code-discovery setup.
-----------------------------------------------------------------
Editable section                         Optional guidance
[Known values + source/time badges]       "I found one accessible repo.
[Resolve only missing/conflicting data]    Use it, choose another, or later?"
[Save section] [Add more later]           [Review suggested changes]
```

```text
Basic / Sources
Provider alternatives: [GitHub] [GitLab] [Other: unavailable]
Accessible connections [existing list]     [Connect another]
Discovered repo ExampleOrg/planner (immutable provider ID retained)
Language Python · CI existing-workflow · observed at <time>
Coverage: partial (100 of unknown total), [Continue discovery]
Team [existing picker]   User mapping [verified identities / unresolved]
No invitation system. [Open existing member assignment] (if permitted)
Conflict: discovered default branch differs from accepted revision
[Keep accepted] [Review source] [Accept discovered]
```

```text
Financials
Amount [decimal] Currency [ISO code]     Period [date] to [date]
Defaults: current Product target; current owner; AI; proposed funding
More details [>] Existing Initiative; optional release target date
Source [Manual / Connected / Extracted proposal] [Inspect provenance]
Proposal: 5000.00 USD, source business-case revision 3
Approval: proposed (confirmation does not approve funding)
[Confirm value] [Override with reason and expiry] [Save later]
Spend/forecast: unavailable (no finance read model); never display 0.
```

## Common states and accessibility

| State | Required visible behavior |
|---|---|
| Loading | Skeleton labelled loading, disable duplicate submit, retain unsent draft locally; never briefly display another tenant's cached values. |
| Empty | Explain no selected/accessible sources and offer existing connect/selection paths; do not claim no assets exist at provider. |
| Partial | Per-source coverage and cursor; save available facts, offer continuation, keep mandatory selected gaps visible. |
| Conflict | Side-by-side accepted/proposed value, actor/source revision/time, explicit keep/replace; no last-write-wins. |
| Error / offline | Preserve typed draft, mark not saved, same operation key on retry; uncertain write links to status instead of submitting a new create. |
| Permission denied | Hide protected values, explain action unavailable, offer authorized handoff where supported; never leak source label in a foreign-scope error. |
| Revoked while open | Clear protected cache/panels, stop edits and polling of hidden data; refetch current access. Historical value does not remain in a toast/download. |
| Pending admin/credential | Persistent operation status and safe same-origin return route. Closing tab does not discard progress. No secret appears in chat or resume link. |
| Downstream unavailable | Profile remains useful; label Context/L5 feature unavailable with specific prerequisite; no fabricated summary, result or successful demonstration. |
| Archive | Read-only authorized history, restore action for owner, explicit external assets preserved; consumer cancellation status may remain pending. |

Keyboard-first controls, visible focus, associated field errors, labelled section
headings, descriptive button text and `aria-live="polite"` save/progress status are
required. Do not use color alone for confirmed/inferred/stale/conflicting badges.
Keep focus on the edited field on validation failure; move to an error summary
only on submit. On async refresh announce changed fields without stealing focus.
Chat transcript has a labelled region and does not replace structured controls.
Reduced motion, 200% zoom/reflow and screen-reader reading order are browser test
assertions, not assumed from component-library choice.

## Three complete journeys

### Idea

Save the minimal form → receive stable Product/Project with the caller's current primary or sole team assigned →
see confirmed information and two core checks complete → optionally choose an
existing repository, or explicitly preview a greenfield repo creation. Creation
requires entitled owner/template revision/visibility/protections and an approved
effect plan; no billable/model call from Save. A new pipeline is a proposed PR,
not silently installed. If no primary or sole team is available, choose a primary
ADP team through existing membership flows before saving. Team members and platform
admins can access the saved Product; ownership alone does not grant access.
Connect a repository to meet Getting Started; leave Financials/Advanced for later.
UI and CLI both finish in a usable saved workspace, not a compulsory wizard gate.

### Existing assets

Save/select identity → enumerate authorized existing connections → discover repo,
branch, languages/build/test entry points and existing CI/document references →
ask only which asset when multiple candidates or a material conflict exists → bind
immutable provider IDs → map already-known team/user identities → resolve remaining
gaps → optional environment/Financials → available downstream setup offer. Providers
are alternatives: selecting GitHub Issues does not require Jira and Linear too.
Repeated discovery does not ask again for confirmed choices or overwrite them.

### Running product

Save identity → select existing sources/environment via verified connection →
read-only inventory and as-is baseline → show observed/reused/gap/unknown/unsupported
states and source timestamps → confirm product boundary and production classification
→ capture support/runbook/recovery references where accessible → retain discovered
pipeline unchanged → optional selected capability handoff. Observe is the starting
mode; Assist/Govern/Operate are separately authorized peer operations, not a Profile
toggle. No production write, role creation or policy change is a discovery side effect.
Unavailable billing/Support/telemetry blocks only the dependent capability.

## Completion, levels and next steps

`profile-1` is a versioned server rule set, not a mutable client score. Each check
has a stable ID, required flag, reason and evidence. The default applicable checks:

| Check | Meaning | Default applicability |
|---|---|---|
| `information` | Name/purpose/users/owner accepted | All starting points, required |
| `project_scope` | Stable authorized governed Project binding | All, required |
| `repository` | At least one currently verified authorized repository | Existing assets and Running product; optional until selected for Idea |
| `planning` | One selected planning source or explicit valid N/A | Added when planning context selected; providers are alternatives |
| `documents` | Selected docs/wiki reference coverage | Added on selection |
| `team_mapping` | Selected team/identity mapping resolved | Added on team selection |
| `environment` | Verified chosen environment connection/topology | Running product or environment-dependent capability |
| `pipeline` | Existing delivery path observed / approved chosen path | Running product or delivery capability |
| `operations` | Selected support/runbook/assurance/recovery context | Running product or operations capability |
| `financials` | Selected allocation fields + provenance, not funding approval | Added on Financials/cost-dependent capability selection |

Denominator is applicable checks after legitimate N/A exclusions; numerator is
complete checks with required fresh/conflict-free evidence; percentage = floor
(100 × numerator / denominator). Minimum denominator 2, so no divide-by-zero.
`later` preserves an applicable missing check rather than removing it. Required
checks for a selected capability cannot become N/A; choosing not to activate that
capability explicitly changes the selected scope and records why the denominator
changed. Information maturity is separate: Saved = core; Getting Started adds
repository; Advanced adds environment+pipeline; Pro adds operations and all selected
required checks. Financials is not universally required to reach a level. None of
these labels means configured, demonstrated, approved or permitted.

Example: an Idea has 2/2=100%, maturity Saved. Selecting code setup adds repository:
2/3=66%, then a verified binding yields 3/3=100%, Getting Started. Disconnect makes
it 2/3, capability attention required; no cached 100% grants use. Hints choose at
most two missing selected checks, then useful optional discovery/setup offers;
they explain benefit/prerequisite rather than claim automatic activation.

### Section state and freshness

The four `Session.steps` summarize saved checks, not whether a tab was visited.
Information covers `information` and `project_scope`; Connections covers
`repository`, `planning`, `documents` and `team_mapping`; Environments covers
`environment`, `pipeline` and `operations`; Financials covers `financials`.
Return qualified evidence references for completed checks only.

An outstanding section operation takes precedence as `waiting_external`, with its
operation ID and next action; failed work needing intervention is `blocked`.
Otherwise an untouched optional section is `not_started`, a wholly excluded
section is `not_applicable` with a reason, and fresh satisfied checks yield
`complete`. Stale evidence is `blocked`; only deferred outstanding checks yield
`later`; other incomplete selected checks yield `in_progress`. A completed session
may contain untouched optional sections. Waiting, blocked, later or in-progress
sections cannot be reported as a completed session. Pending suggestions stay
available independently and never certify a saved check.

Use the same freshness decision in source cards, review dialogs, binding exports,
Context source counts, progress and service readiness. Expired evidence needs
verification again, retaining its original observation time. Current access loss
still redacts source details. Refresh a clean form when the aggregate revision
changes; preserve dirty/offline drafts and uncertain saves for deliberate conflict
review or receipt reconciliation. Creation and edits both enforce the API limits:
name/domain 200 characters, purpose 10,000, and 1–100 distinct intended users of
at most 200 characters each, after trimming.

## Exact proposed CLI tree and flags

The delivery-scope label in the UI does not rename the canonical `project_id` or
the CLI's `--project` flag.

Extend `adp` dispatch, `adp-product.py`, `adp-onboard.py`, `command-manifest.json`,
installer/updater file lists and `src/cli_download/routes.py` serve allowlists.
These two providers are **proposed paths**. Reuse `adp_common.py` transport, private
state, output, capability preflight and existing tenant/deployment selection (S08).
No new installer or client credential cache. All commands accept `--json`.

Common scope flags: `--product ID --project ID` required except list/create and
explicit project selection. Common mutation flags: `--idempotency-key KEY` and
`--revision N`; interactive mode may obtain them from the just-read session,
noninteractive mode requires them (create needs no revision). `--input FILE|-`
reads the exact typed JSON body. `--yes` confirms an already displayed/declared
choice; it is not authorization. `--dry-run` validates locally and reads current
scope; it performs no server mutation/provider effect and reports the intended
operation and required approvals. It cannot prove live write permission.

| Command after `adp` | Command-specific flags / shared backend operation |
|---|---|
| `product list` | `--cursor TOKEN --limit 1..100` → listProducts |
| `product create` | `--input FILE\|-`, or `--name TEXT --purpose TEXT --intended-user TEXT` (repeatable) `--starting-point idea\|existing_assets\|running_product` and `--new-project` or `--existing-project ID`; optional `--owner ID`, `--team ID` (Product mapping, inherited by Projects) → createProduct |
| `product show` | scope, optional `--revision N` → getProfile/getRevision |
| `product update` | scope, mutation flags, `--input FILE\|-` → updateProduct |
| `product project list` | cursor/limit → listProjects |
| `product project bind` | `--product ID`, mutation flags, `--input FILE\|-` ProjectSelection → bindProject |
| `product project update` | scope, mutation flags, `--input FILE\|-` → updateProject |
| `product project unbind` | scope, mutation flags, `--yes` → unbindProject |
| `product lifecycle` | scope, mutation flags, `--action archive\|restore --reason TEXT --yes` → changeLifecycle |
| `product sources` | scope, cursor/limit; for asset continuation `--connection-kind github_installation\|credential --connection-id ID --resource-cursor TOKEN` together → listSources |
| `product binding list` | scope, cursor/limit → listBindings |
| `product binding add` | scope, mutation flags, `--input FILE\|-` → createBinding |
| `product binding remove` | scope, mutation flags, `--binding ID --yes` → unlinkBinding |
| `product binding verify` | scope, mutation flags, `--binding ID` → verifyBinding |
| `product history` | scope, cursor/limit → listChanges |
| `onboard start` | scope, mutation flags, `--guided` optional; `--input FILE\|-` optional SessionStart → startOnboarding |
| `onboard resume` | scope, `--session ID`, `--guided` optional → getOnboarding; no writes until explicit action |
| `onboard step` | scope, mutation flags, `--session ID --section information\|basic\|advanced\|financials --input FILE\|-` → updateStep |
| `onboard discover` | scope, mutation flags, `--session ID --binding ID` repeat ≤10, optional `--cursor TOKEN` → discoverSources |
| `onboard guide` | scope, mutation flags, `--session ID --input FILE\|-` GuidanceRequest → guideOnboarding; `--guided` loops through this same operation; request `mode` defaults deterministic, `model` explicitly opts into metered assistance |
| `onboard handoff` | scope, mutation flags, `--session ID --input FILE\|-` → startHandoff |
| `onboard operation show` | scope, `--operation ID` → getOperation |
| `onboard operation reconcile` | scope, mutation flags, `--operation ID` → reconcileOperation |
| `onboard operation cancel` | scope, mutation flags, `--operation ID --yes` → cancelOperation |

`--input` and equivalent inline body flags are mutually exclusive. JSON/non-TTY
never prompts or opens a browser; missing choice returns usage error with needed
field names. Guided mode uses optional model only after budget/consent preflight;
deterministic missing-field guidance and direct commands remain available when
model service is unavailable. Agent tools expose these same operation IDs, record
the delegation/actor and show the exact proposed mutation before confirmation.

JSON output retains the existing envelope (S08):

```json
{"status":"pending","command":"onboard handoff","detail":{"operation_id":"00000000-0000-4000-8000-000000000003","revision":7,"state":"waiting_external"},"next_action":"Complete the connection in Settings, then resume this session."}
```

`detail` contains the operation's typed result; failures add
`error:{code,message}`. Do not wrap arbitrary peer payloads. stdout is one JSON
object, progress/prompts go to stderr. Exit codes preserve existing conventions:
0 succeeded; 1 usage/validation; 2 authentication; 3 permission; 4 pending,
unavailable dependency, stale revision or unknown mutation outcome; 5 unsupported
operation/protocol/other failure. API 404 maps to 5 without revealing whether the
resource exists. A local unsupported feature returns 5; an available operation
whose downstream dependency is temporarily unavailable returns 4. No new exit
codes or silent shell retries are introduced.

## Cross-surface transcripts (SCN-02)

These are proposed examples, not commands run in this review. IDs are fictional.
Prerequisites after implementation: installed compatible CLI, existing ADP login,
same selected deployment and tenant as browser. Replace IDs with returned values.

**UI → CLI:** Create Idea in browser; start guidance. UI saves revision 2 and
displays session ID plus Copy resume command. Close the browser before connecting
any source. In a terminal:

```bash
adp onboard resume --product 00000000-0000-4000-8000-000000000001 --project 00000000-0000-4000-8000-000000000002 --session 00000000-0000-4000-8000-000000000004 --json
```

Expected: same confirmed name/purpose/owner, revision 2, no repository, original
check denominator. Use `product sources` to select existing metadata without
re-entering IDs. CLI edit at revision 2 becomes revision 3. Browser refresh shows
revision 3; its unsaved old revision cannot overwrite it (412).

**CLI → UI:** Save minimal identity without a model:

```bash
adp product create --name 'Example Planner' --purpose 'Coordinate small releases' --intended-user Maintainers --starting-point idea --new-project --idempotency-key example-create-idea-0001 --json
```

Start a session with returned scope/revision, choose a credential handoff, then
interrupt the terminal. Open the returned same-origin onboarding `resume_path` in
the browser after authenticating. UI loads server progress, completes the existing
connection flow, and displays a verified binding or a truthful pending/denied
state. CLI resume reads that same binding and does not repeat questions.

**Credential/admin handoff:** For an unavailable AWS connection the guided UI uses
the existing connect screen; the CLI can direct an authorized operator to existing
`adp aws connect --download DIRECTORY` and later `adp aws connect --resume DIRECTORY`
with the already selected connection context. The existing command's required
account/region/setup inputs must be discovered when available; missing target
details require an explicit choice. These flags are existing (S08), not a Product
implementation of role provisioning. The administrator runs their authorized
setup separately, never gives the member credentials. The Product operation keeps
only its ID and the eventual verified credential reference. GitHub uses existing
`adp github connect --repo OWNER/NAME --no-browser --json` / Settings flow; install
receipt is verified by the connection service before the session can proceed.

Private local resume cache uses existing deployment-specific `state_dir()` and
tenant/actor metadata checks, mode 0700 directories/0600 files and atomic writes.
It holds only IDs, revisions, operation keys and safe navigation state. Server state is
authoritative; deleting cache cannot lose confirmed work. A changed deployment or
tenant refuses cached resume; user must select the correct authenticated scope.
No token, secret, credential value or OAuth state is printed in a resume command.

## Reviewable simplicity and state examples

The HTML demonstrates a three-field first save, a default first Project and four
optional sections. Product identity/team stays shared while switching between
Projects loads separate connections, financials, pending proposals and resume IDs.
Projects are delivery/evidence scopes; Initiatives normally span Products via
explicit Contributions. Release planning is optional and separately owned.

The list shows Product lifecycle and authorized Project count. The selected Project
shows completeness. A minimal Idea is 2/2, 100%, Saved; connecting a repository is a
useful next option rather than a hidden mandatory onboarding step. Selected unverified
connections stay incomplete. Environment + pipeline are needed for Advanced and
operations for Pro. Optional Financials can be deferred or excluded with a reason.

Financial entry shows amount, currency and a suggested editable three-month period.
Target, owner and proposed funding are visible defaults; an existing Initiative and
release date are optional details. Import uses field selection, leaves manual values
unselected by default, and retains unconsumed suggestions across browser reload or
CLI resume. A manual edit changes only that field's provenance.

Preview scenarios exercise partial/unavailable/denied discovery, pending credential
or administrator work, source revocation, offline drafts, lost save confirmation
and a concurrent CLI edit. Product screens use ordinary messages and actions;
request IDs and revision mechanics belong in the CLI/export or review controls.
The conflict dialog shows the saved and edited values and requires an explicit
choice; saving edits retains unrelated current values.


### Workspace implementation: people and navigation

Overview shows the product purpose, actions, saved progress and People & access.
Profile setup groups Product information, Connections & people, Environments &
delivery, and Financials. The current setup section survives a reload in the same
signed-in session. Hidden sections retain unfinished edits. Context & services
shows the reported unavailable state when integration is deferred. CLI commands
and record identifiers are available under saved details.

People & access uses `getProductPeople`. Organization administrators can choose a
Product team and owner; platform administrators can also enroll a GitHub account.
An existing ADP member can be added through the shared team membership API. The UI
explains that adding a person affects other products shared with the team. It does
not imply that a repository collaborator automatically receives Product access.
The Product list shows the team and scope count, and the scope selector uses
Project names. A single scope does not require a selector.
