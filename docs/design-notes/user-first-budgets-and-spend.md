# User-first Budget & Spend

Status: implemented in `feat/user-first-budget-spend`, 2026-09-14. Not deployed.

## Implementation

- `/budget` now combines My spend and authorized budget management. `/budgets`
  redirects to `/budget?view=manage`; unauthorized callers see My spend.
  `/model-access` retains Bedrock routing administration.
- `GET /me/budget/monthly-spend` reads the person's monthly and daily ledger rows
  in one statement. Missing daily reconciliation produces an explicit unavailable
  breakdown, while the monthly total remains visible. Dates and reset use UTC.
- Platform-admin-only `GET /budget/hierarchy` retains organizations, teams and
  all human memberships, including empty teams and users without a team.
  `GET /budget/people-spend` deduplicates people before search and pagination.
  Existing person-cap/default write APIs handle all contextual budget editing.
- Budget resolution includes secondary team memberships. Native user anchors are
  validated and supported by both enforcement branches as well as reads/writes.
  The individual → team → organization → platform order remains unchanged.
- Existing workspace/daily/weekly controls remain under Additional restrictions.
  No existing budget rows were automatically migrated, deleted or rewritten.

Validation: the budget and identity backend suite passed 878 tests (2 skipped),
plus the new removal-fallback integration test. 208 focused frontend tests passed;
TypeScript, production build and changed-code lint passed. Browser checks used
fixture responses against the production build at desktop and 390px mobile widths,
covering daily rows, a single navigation entry, nested editing, fallback previews,
Escape, overflow and the legacy URL redirect.

Current limits: account information describes current effective user routing;
per-run destination history and a full AWS infrastructure bill are not available
from this API. Hierarchy construction reads the directory before resolving people;
the person spend report pages the deduplicated result, and only reads spend for the
requested page. Existing policy migration remains a separate reviewed operation.


Interactive example: [Budget & Spend mockup](../mockups/user-first-budgets-and-spend.html).
All names and amounts in the example are fictional.

## Product model

**Each person has one effective monthly budget. The most specific configured
level supplies it: individual → team → organization → platform default. Their
direct model usage and the cloud agents they initiate consume that same budget,
wherever the work runs.**

A team or organization budget here assigns a monthly amount to each person who
inherits it. For example, an organization default of $750, a team default of
$500, and an individual budget of $1,000 resolve to $1,000 for that individual.
Removing the individual budget makes the team’s $500 apply; removing the team
default makes the organization’s $750 apply. A more specific amount overrides
its ancestors whether it is higher or lower. The amounts are never added.

Skip levels with no configured amount. If none of the levels has a budget, show
“No budget set”. Removing an override means inheriting the next available
default; it does not automatically make the person unlimited. Team/organization
defaults are per-person amounts; separately configured pooled spending controls
retain their own explicit meaning and administration surface.

For people with several memberships at the same level, retain the existing
resolver’s tie rule: use the lowest amount among applicable rules at that level,
with a stable source for equal amounts. This does not change the precedence
between levels. Use ADP organization/team membership to resolve policy; GitHub
connections and the active workspace do not select the budget.

For example, $84.20 of direct usage and $163.30 from cloud agents means
**$247.50 spent of a $500 monthly budget**, with $252.50 remaining.
Switching GitHub organizations or ADP workspaces does not change these numbers.

ADP organizations and teams retain their administrative hierarchy:
**Organization → Team → User**. Budget configuration follows that structure;
the closest configured budget is inherited down the branch. Each team remains
inside its parent organization, and its users remain inside that team. The
personal spend view resolves this hierarchy to one amount automatically.
Repositories, GitHub connections, and AWS accounts describe where work happens
or where it is billed; they do not replace the ADP organization/team hierarchy.

Here, spend means the model usage recorded by ADP. Cloud-agent spend is the model
usage attributable to those agents; this proposal does not claim to include EC2,
EKS, storage, or the complete AWS bill. Those costs need an explicit accounting
design before inclusion in this budget.

## One navigation entry: Budget & Spend

Consolidate the personal spend screen and budget management under one menu item
and one page title: **Budget & Spend**. This is the destination for understanding
spend, seeing the effective budget, and managing budgets where authorized.

- **My spend** is the default view for everyone, including admins: the Bedrock
  account callout, one monthly spend/budget summary, daily breakdown, and budget
  source details.
- **Manage budgets** is an additional tab for authorized admins. It contains the
  expandable Organization → Team → User budget hierarchy and a spend-by-person
  report. Budgets are edited at the relevant node in the hierarchy. These
  controls stay on this page.
- Users see their own read-only budget without admin controls. An admin can
  switch between their personal usage and management without leaving the page.
  Existing platform-admin authority for global totals and person-budget writes
  remains enforced on the server. An org admin does not gain that authority just
  because the menu is shared.
- Remove duplicate navigation entries named “My spend”, “Budgets”, or “Budget
  Management”. Keep existing links working by redirecting `/budgets` to the
  authorized management view of `/budget`; unauthorized callers fall back to
  My spend. A management deep link must enforce the same access rules as the tab.
- Account routing configuration stays in Model access; the account callout on
  My spend identifies where Bedrock usage is going.

The mockup's yellow “Preview role” selector only demonstrates visibility. It is
not a proposed user-facing role switch or an authorization mechanism.

## My spend tab

- Above the spend summary, show **Bedrock billed to · Current AWS account** with
  the friendly account name, full 12-digit AWS account ID, and effective routing
  source, such as “Team default · Platform”. State that this account serves new
  Bedrock requests. This is read-only account information on the spend screen;
  account configuration stays in Model access.
- If direct usage and cloud agents resolve to different accounts, name both with
  their usage labels. Their costs still contribute to the same personal budget.
  The source of the AWS account is resolved independently from budget inheritance;
  changing an individual budget must not appear to change the billed account.
- Current routing does not establish where historical spend was billed. When
  actual account history shows a change this month, note the change and disclose
  the earlier account in details. Preserve attribution from the actual request
  or run; do not relabel previous charges using today's account assignment.
  If account details cannot be read, say so while keeping available spend and
  budget figures visible. A platform fallback with an unavailable ID must be
  labelled honestly rather than assigned a guessed account number.
- One summary: **$247.50 spent of $500 this month**, a progress bar, remaining
  amount, and reset date. Remaining is arithmetic derived from the same budget.
- A small breakdown within that summary: **Direct usage $84.20** and
  **Cloud agents $163.30**. These are contributions to the total, with no separate
  budgets or progress bars.
- The expandable **Usage breakdown** shows a table for the selected month:
  **Date | Direct usage | Cloud agents | Total**. Show one row per elapsed day,
  most recent first, including days with zero usage. Mark today as in progress;
  future dates do not appear as zero spend. Use UTC day boundaries to match the
  budget period. A month-to-date footer reconciles with the headline and both
  component amounts. These are daily spend figures, with no daily budgets.
- Repositories, runs and organization reporting can be further drill-downs.
  Organization rows and organization caps do not lead the page.
- The monthly budget is read-only for the user, matching current admin ownership.
  A short “Managed by your platform admin” line replaces the policy explanation.
- Budget provenance, such as an individual override or team default, belongs in
  details. It is a source of the one budget, not another budget to manage.
- Historical months can be added when the API supports historical spend and the
  applicable historical budget. Daily/weekly reporting must not silently turn
  into different budget amounts.

Keep failure states clear and short: “Spend unavailable”, “Budget unavailable”,
or “Recent usage is still being priced”. Unknown is never zero or unlimited.
When spend exceeds the budget, show the overage. Advisory limits must say that
they are advisory; settled usage does not promise an instantaneous hard stop.

## Manage budgets tab

Lead with an expandable **Budget hierarchy**, retaining the existing ADP
parent/child structure. The platform default is the outer fallback:

```text
Platform default                              $300
├─ Engineering (organization)                  $750  set here
│  ├─ Platform (team)                          $500  set here
│  │  ├─ Alex Morgan                          $500  inherited from team
│  │  └─ Jordan Lee                         $1,000  individual override
│  └─ Delivery (team)                         $750  inherited from organization
│     └─ Sam Patel                            $750  inherited from organization
└─ Operations (organization)                   $300  inherited from platform
   └─ Support (team)                          $300  inherited from platform
      └─ Taylor Reed                          $300  inherited from platform
```

Each row shows its effective monthly amount per person, whether it is set there
or inherited, and **Set budget** / **Edit budget**. Setting a team budget happens
on that team's row inside its organization. Setting an organization budget
happens on its parent row. Setting an individual override happens on the user
inside their team. The editor keeps the full organization/team/user path visible
and previews the parent fallback when an override is removed.

Changing a node updates descendants that inherit from it. An explicit budget on
a descendant continues to override the parent, whether higher or lower. Sibling
teams and unrelated organizations retain their own applicable budgets. The
tree includes existing organizations and teams with no explicit budget so an
admin can set one in place. Expanding/collapsing a branch does not change scope,
membership, or the effective budget. Budget editing does not reassign membership.

Below the hierarchy, **Spend by person** has one row per person, deduplicated
across memberships. It reports each person's combined usage and resolved budget:

| User | Direct usage | Cloud agents | Total spent | Monthly budget | Remaining |
|---|---:|---:|---:|---:|---:|
| Alex Morgan | $84.20 | $163.30 | $247.50 | $500.00 | $252.50 |
| Sam Patel | $62.00 | $348.00 | $410.00 | $750.00 | $340.00 |

Search the spend report by person. Each budget cell shows the one effective
amount with a small source label, such as “Team · Platform” or “Organization ·
Engineering”. Spend is attributed to the person, while the hierarchy above shows
where their budget comes from. If multiple memberships place a person in more
than one branch, each appearance refers to the same individual budget; the spend
report still counts that person once. No raw entity IDs or GitHub organization
picker in the budget authoring flow. Account routing stays in Model access.

The all-users table is for platform admins under current authority. An org admin
must not gain another organization's spend merely because a person belongs to
both. Any org-scoped report should identify its scope and must not present a
partial spend total as utilization of the person's complete budget.

Unattended automation with no attributable person needs its own visible
automation account. It must not disappear from reports or be arbitrarily charged
to the repository owner or the admin who installed the connection.

## What already exists

Verified against `origin/main` at `5118f223` on 2026-09-14. The working checkout is
an older branch; these findings describe current main, not a verified deployment.

| Current implementation | Implication |
|---|---|
| [`me_routes.py`, `_person_envelope`](https://github.com/aws-e/adp/blob/5118f223/modules/gateway/src/budget/me_routes.py#L693) returns `spend_usd`, `direct_spend_usd`, and `cloud_spend_usd` across the person's partitions. | The personal summary and its two components already have a server read model. |
| [`person_ledger.py`](https://github.com/aws-e/adp/blob/5118f223/modules/gateway/src/budget/person_ledger.py) supplies shared identity, spend, and applicable-limit resolution used by reads and enforcement. | Reuse this definition. Do not sum arbitrary user/org ledger rows or duplicate the budget resolver in the client. |
| [`bedrockRouting.ts`, `EffectiveMappingResponse`](https://github.com/aws-e/adp/blob/5118f223/modules/gateway/frontend/src/types/bedrockRouting.ts) provides `account_id`, `destination_label`, and the effective routing rung; the self-service response carries this as `effective`. | Use the effective destination for the account callout. The saved selection may be inactive. This contract alone does not establish historical billing or confirm every cloud execution context. |
| [`SpendTiles.tsx`](https://github.com/aws-e/adp/blob/5118f223/modules/gateway/frontend/src/components/budget/SpendTiles.tsx) shows a personal total, then “by GitHub org” and “direct use & other lines”. | The combined total exists, but the details reintroduce the competing organization model. |
| [`PerOrgSpend.tsx`](https://github.com/aws-e/adp/blob/5118f223/modules/gateway/frontend/src/components/budget/PerOrgSpend.tsx) displays “Cap here” beside each org's direct and cloud spend; that cap covers cloud spend only. | Side-by-side placement invites the mistaken reading that the cap governs both columns. |
| [`BudgetManagement.tsx`](https://github.com/aws-e/adp/blob/5118f223/modules/gateway/frontend/src/pages/BudgetManagement.tsx) combines entity budgets, default person limits, and account routing. | Replace the primary admin flow with people and their resolved monthly amounts. |
| [`person_cap_routes.py`, `_compose_member_budget`](https://github.com/aws-e/adp/blob/5118f223/modules/gateway/src/budget/person_cap_routes.py#L1208) pairs an org-only spend figure with an applicable person limit. | This row cannot be reused as the person's complete spend. A global admin list needs a deduplicated person read across authorized partitions. |

## Moving to one budget without unexplained stops

This requires both presentation changes and a policy migration. Existing daily,
weekly, org-scoped direct-user, and org-scoped cloud-user limits can still stop
work independently of the monthly person budget. Hiding those numbers alone does
not produce the promised behavior.

1. Build the personal summary using the existing person envelope and resolved
   person limit. Surface applicable legacy constraints in clearly labelled
   details during transition, with an explicit reason when one blocks work.
   Add a daily person-spend read for the selected month, using the same identity,
   attribution and partition rules as the summary. The monthly aggregate alone
   cannot supply historical daily rows: read actual dated usage, with aligned
   pricing/as-of boundaries so the daily sums reconcile with the headline.
   Report unavailable or pending data explicitly; do not derive daily spend by
   distributing the monthly total. The mockup uses fictional daily fixtures.
   Read the effective Bedrock account separately so a routing-read failure does
   not hide budget data. Verify direct and cloud destination coverage before
   labelling both with one account. Use recorded account attribution for any
   history; do not infer previous accounts from the current routing response.
2. Add a platform-admin list API that paginates people rather than memberships,
   and reads each person's complete spend and resolved limit using the shared
   resolver. Use it for the table and single-amount editor. Cover people without
   GitHub identities, as well as linked identities.
3. Produce a migration report of every affected user's monthly limit, daily and
   weekly limits, per-org person caps, source rules, and proposed resulting
   monthly budget. Reuse an existing monthly person budget where present; report
   conflicts. Do not add old caps together or convert weekly limits to monthly
   amounts by multiplication.
4. Apply reviewed monthly person budgets and retire redundant personal caps as a
   controlled policy change. Preserve the audit trail and a rollback mapping.
   Stop offering the old personal-cap types in the normal create form.
5. Keep any intentional shared organization/team spending guardrails in admin
   policy settings. They are pooled controls and cannot be folded into a fixed
   personal budget: colleagues consume that pool too. If a shared guardrail
   blocks work, state the reason at the affected action and on My spend. The
   personal remaining amount must not promise unconditional permission to spend.

## Acceptance criteria for implementation

- Exactly one **Budget & Spend** menu entry is shown. Both tabs retain the same
  page title and active menu item. My spend is the initial view; Manage budgets
  is offered only with the required authority, including for direct links.
  Old budget URLs continue to resolve without restoring duplicate screens.
- A person sees the same monthly total and budget regardless of active workspace.
- The AWS account callout names the effective Bedrock destination, including its
  account ID and routing source. It survives spend-read failures, and its own
  failure does not hide available budget/spend data. Differing destinations are
  shown separately; current account settings do not rewrite historical billing.
- The most specific configured budget wins: individual → team → organization →
  platform default. Verify overrides both above and below the parent amount,
  missing levels, removal fallback, and the absence of every applicable budget.
  Multiple memberships use the existing deterministic tie rule within a level.
- Each surface reports the same effective amount and source. A team or org
  default applies separately to each inheriting user, and editing it preserves
  more specific overrides. It is not consumed as a shared pool by these users.
- The budget editor retains Organization → Team → User nesting, with the full
  parent path visible when editing. Teams are shown only under their own
  organization and users under their memberships. Org/team nodes without an
  explicit budget remain visible and offer Set budget. Parent changes update
  inheriting descendants, preserving explicit overrides and unrelated branches.
- Expanding/collapsing the hierarchy preserves its state after budget edits.
  The spend report remains per person and deduplicated across memberships.
- Direct plus cloud components reconcile exactly to the server's person total;
  hosted-worker rows and org rollups are not counted again.
- Daily rows reconcile to both monthly component amounts and the combined total,
  using the same UTC month and pricing snapshot. The current day is partial;
  no-usage days are zero, unavailable figures are unknown, and future days are
  omitted. Daily reporting does not introduce daily budget limits.
- Both direct requests and attributed cloud-agent requests consume and check the
  same applicable person budget, including linked identities and non-GitHub users.
- Editing one person's budget changes that amount everywhere, without creating
  separate direct/cloud/org budgets. Reset-to-inherited shows the resulting
  amount and whether it comes from the team, organization, or platform.
- The admin list has one row per person; pagination, search and totals cannot
  duplicate a person because of multiple memberships.
- Unknown spend, unpriced usage, no budget, an advisory budget, an overage and an
  active shared restriction each have distinct, accurate states.
- No org admin gains cross-org totals or budget-write authority through the UI.
- Existing additional caps remain discoverable until explicitly migrated; the
  monthly-only end state is not declared while hidden personal caps still apply.

The mockup demonstrates the daily breakdown, budget editing and exception states
with fictional data. It does not connect to the APIs or implement the migration.
