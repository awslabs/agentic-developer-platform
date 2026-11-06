# Agentic Engineering Insights: What It Is, How It Works, and What It Measures

**Purpose of this document:** a single reference for what Agentic Engineering Insights is, how it works, and which metrics it should capture so that AI spend in software delivery can be tied to results. It's written for the AIM319 speakers, for anyone building it, and as a basis for the take-home resource. Section 6.1 defines the six outcome metrics for this use case's AI ROI Scorecard, including their calculation, customer benefit and progression up the autonomy ladder.

**Lineage detail:** [Agentic Engineering Insights — Lineage Schema](lineage-schema.md) describes the proposed record fields, identifier sources, relationships and validation checks. Section 5.3 below gives the high-level lineage. This overview owns the six scorecard metric definitions and evidence rules; the schema maps them to the supporting records. The schema is a design reference, with implementation decisions still open.

**Worked scenario:** [section 14](#14-worked-scenario-business-case-to-roi-scorecard) follows a retailer's checkout modernisation investment through the five ROI modules, enterprise systems, lineage evidence and final scorecard decision.

---

The [requirements/test analytics v2 extension](lineage-schema.md#11-requirements-and-test-analytics-v2)
adds immutable story/criterion revisions and separates pack runs, planned tests,
results and acceptance assessments. The [current storage decision](architecture.md#31-requirements-and-test-analytics-storage-decision)
uses S3 Parquet and DuckDB for that initial query path. The S3 Tables/Athena
examples below remain the broader ROI target, not a prerequisite or a claim of
implemented analytics. The six outcome metric definitions are unchanged.

## 1. In one paragraph

Agentic Engineering Insights is the measurement part of software delivery when agents do some or most of the work. It collects two kinds of data, **what the work cost** and **how the work went**, links both to the same unit of work (the story), and turns them into a small set of metrics that stay the same as a team moves from agents on a laptop to agents running releases. Its job is to answer one question with evidence: **is agent work making software delivery better, and is it worth what it costs?**

---

## 2. Why it's needed

Three problems make AI value in software delivery hard to show today.

1. **The bill doesn't say what it bought.** Token counts and the AWS bill show how much was spent and, at best, which team spent it. They don't show which story the money went into, or whether that story shipped, broke anything, or had to be redone.
2. **Faster isn't always better.** Industry data shows agents raising output while quality slips. Faros AI's 2026 report found epics completed per developer up 66%, but the ratio of incidents to PRs up 242.7% under high AI adoption. DORA research found delivery stability falling as AI adoption rose. Measuring speed or cost on its own gives a misleading picture.
3. **Agent work and human work look the same.** Without tagging at the source, you can't compare them, so you can't decide where agents should be trusted with more.

Engineering Insights fixes these by joining cost and delivery data on the same IDs, at the moment the data is created rather than after the fact.

---

## 3. Where it sits

### 3.1 In the three parts of every AI workload

| Part | Job | For software delivery |
|---|---|---|
| Execute | Does the work | ADP, coding agents, cloud agents, delivery engine |
| Control | Sets the limits | ADP gateway, AI Budget Controller, quality and promotion gates |
| **Measure** | **Shows whether the work was worth it** | **Agentic Engineering Insights** |

### 3.2 In the AI ROI stack

The AI ROI stack has five shared modules. Engineering Insights is the **AI Outcome Tracker, set up for software delivery**, together with its connection point, the **AI Outcome Metrics Collector**.

| Module | How it relates to Engineering Insights |
|---|---|
| AI Business Case Registry | Supplies the goal, targets, unit of work (story) and boundary. Engineering Insights measures against these. |
| AI Budget Controller | Supplies reconciled cost per call and per story. Engineering Insights reads this; it doesn't count spend itself. |
| **AI Outcome Tracker** | **This is Engineering Insights.** It turns delivery and quality events into metrics using the software delivery metric pack. |
| AI ROI Scorecard | Reads Engineering Insights' metrics and compares them with the business case targets. |
| AI ROI Lake | Receives a standard feed from Engineering Insights so this project can be compared with others. |

### 3.3 What it is not

- **Not FinOps.** It doesn't manage spend, set budgets or do chargeback. Cost is one input, used to work out cost per unit of work.
- **Not an enforcement system.** It doesn't block calls or merges. The gateway and the gates enforce; Engineering Insights feeds them information.
- **Not business analytics.** It measures software delivery and the quality of shipped changes. Conversion, adoption and revenue are measured by product analytics and only come in as a read-only feed.
- **Not a developer ranking tool.** It reports at team level by default.

---

## 4. The boundary

Engineering Insights measures **software delivery through production, including post-release quality**. Lead time starts at the first commit and ends at the first successful production deployment that completes the story. Cost and human effort cover the agreed delivery scope, including planning, implementation, review, validation and corrective work. Change failures are observed for 7 days after deployment, and rework for 30 days after a story ships. These windows remain fixed across autonomy stages.

Product analytics answers whether the feature changed customer behavior or business results. Engineering Insights provides the delivery evidence and shares release IDs so the Scorecard can bring those outcomes together.

| Inside the boundary (Engineering Insights) | Outside the boundary (other systems) |
|---|---|
| Cost to build each story, epic and release | Feature adoption, conversion, retention |
| Lead time, throughput, change failure rate, rework | Revenue, customer satisfaction |
| Tests, coverage, security findings, reviews, gates | Support volume after launch |
| Agent vs human comparisons | Market or competitive effects |

The two sides are joined in the Scorecard using a shared **release ID or feature flag name**, not by Engineering Insights calculating business metrics.

---

## 5. How it works

### 5.1 The five steps

Every event goes through the same five steps. The audience-facing version is on slide 20 of the deck.

| Step | What happens | Data check at this step |
|---|---|---|
| **1. Collect** | Events arrive as they happen through webhooks (GitHub, CI, Jira or Linear, deployment pipeline, incident tool) and from the ADP gateway and AI Budget Controller. A nightly job fills any gaps and loads history for the baseline. CI uploads test, coverage and scan reports directly. | Required fields present; duplicates removed using the source's delivery ID |
| **2. Link** | Each event is stamped with the IDs that tie it to work: story ID (from the branch name, the PR, or the agent run that the story started), task ID (one ADP session or agent run), epic ID and release ID. Dated mappings connect that work to its product, optional initiative and business case within the authorized Insights project. Anything that can't be linked is set aside and counted. | Linked rate shown; missing business context and orphans counted separately |
| **3. Keep** | Every raw record is stored and never edited: every test run, scan, review, gate result, agent run and call cost. Corrections are added as new records. | Append-only storage; any number can be traced to its records |
| **4. Measure** | Metrics are calculated from the kept records using written, versioned definitions, and compared against the baseline and between groups (teams onboarded vs not yet, agent work vs human work). | One definition per metric, versioned; history recalculated if a definition changes |
| **5. Share** | Results go to dashboards, the AI ROI Scorecard and the AI ROI Lake, and back into Control: cost history to the Budget Controller, quality results to the gates, release results to autonomy decisions. | Totals checked against the AWS bill and the pipeline's own records every day |

### 5.2 The eight components behind the five steps

| # | Component | Step | What it does |
|---|---|---|---|
| 1 | Sources | Collect | Cost: ADP gateway, AI Budget Controller (reconciled with CUR 2.0). Delivery: GitHub, CI, test and coverage reports, security scanners, Jira or Linear, deployments, incidents. Identity: developer → team. |
| 2 | Collector | Collect | Receives webhooks, checks signatures, removes duplicates, runs the nightly catch-up, accepts CI uploads |
| 3 | Linker | Link | Attaches story, task, epic and release IDs; resolves product, optional initiative and business-case links; quarantines unresolved required links |
| 4 | Evidence store | Keep | Every raw record, append-only |
| 5 | Work records | Keep | One current record per story, epic and release, built from the evidence |
| 6 | Metric engine | Measure | The software delivery metric pack: core metrics plus stage detail |
| 7 | Baselines and comparisons | Measure | The "before" picture; cohorts (rollout waves, agent vs human) |
| 8 | Publishing | Share | Dashboards, Scorecard feed, Lake feed, and the feeds back into Control |

### 5.3 Lineage at a glance

Lineage starts with the **product or service** the work improves. It connects that product and its investment case to the work performed, its cost and the evidence of what shipped. The **story** joins the work to its intended outcome. The **commit** joins the work to the exact code that was tested and released.

```text
Scope around every record: Tenant + Insights Project

Portfolio ↔ Product (optional durable collections)
Initiative ↔ Product (optional, many-to-many)
Contribution = Initiative × Product × governed Project commitment
Contribution → selected Epic / Story
Product ↔ Epic → Story → Task
Business Case ── funds ──→ Initiative and/or Epic

Routine work can omit Initiative and Epic: Product ↔ Story → Task
```

| Identifier | Meaning | Example |
|---|---|---|
| `portfolio_id` | Optional durable collection of Products for investment and reporting | Commerce contains Checkout and Accounts; Shared services also contains Accounts |
| `product_id` | The enduring product or service whose delivery investment and outcomes we track | `PRD-CHECKOUT` — Checkout |
| `initiative_id` | An optional business objective that normally spans products | `INIT-2026-014` — Checkout modernisation |
| Contribution reference | One Product's agreed outcome and existing work within a governed Project | Checkout: faster purchase flow; Accounts: sign-in changes |
| `project_id` | Canonical persistent delivery/operating scope, also the Insights evidence and access boundary | `INS-PROJ-01` — Checkout delivery scope |

These are relationships, not a required parent tree. Each Initiative can span
Products, and each Product can join several Initiatives. Contributions connect the
agreed Product outcome to existing work without creating another backlog. Routine
work can link directly to a Product. Product and Initiative links support reporting;
access remains governed by Tenant + Insights Project. The Profile registry's v1
rule of one managing Product per Project governs configuration, not exclusive
attribution of Stories or Releases.

Portfolio membership is also optional and many-to-many, with dated history.
Products can participate in Initiatives across Portfolio boundaries. A Program
groups Initiatives and remains distinct from a Portfolio of Products. Portfolio
views deduplicate shared work/cost and respect each Project's access; overlapping
portfolio summaries cannot be added together. None of these groupings adds a
required field to Product creation.

| Evidence path | IDs that connect it | What the connection explains |
|---|---|---|
| Product and delivery work | Initiative ↔ Product via Contributions; explicit Product/Contribution ↔ Work; Story → Task | Which product benefits, what it contributes to the shared objective and which run performed the work |
| Investment rationale | Business case ↔ Initiative and/or Epic | Which investment funds the work and which targets apply |
| Spend and human effort | Call → Task → Story; effort records link to the relevant work | What the work consumed, using reconciled cost from the Budget Controller and approved human-effort estimates |
| Acceptance and quality | Story revision → Acceptance criterion revision ↔ Scenario ↔ Test; Run → Planned tests → Results; test runs, scans and reviews link to the Commit or PR | Whether the code satisfies the requested behavior and quality checks |
| Production delivery | Product ↔ Release; planned Story links separate from observed inclusion; Release → Deployment → linked Incident | Which products and work were planned, what artifacts reached each target, when the complete story reached production and whether deployment caused a failure |
| Later business results | Release or feature reference shared with product analytics | Which delivered changes can be associated with externally measured adoption, conversion or revenue |

**Example:** The modernisation Initiative (`INIT-2026-014`), funded by `BC-2026-014`,
has Contributions from Checkout and Accounts. Checkout's Contribution links its
Product (`PRD-CHECKOUT`) and governed Project to epic `CHK-12`, including Story
`PROJ-123`; Accounts links its own sign-in work in a separate authorized Project.
A call belongs to a Task for the Checkout Story. The Task produces a commit, CI
tests it, and a deployment manifest identifies what reached production. The
Products can ship independently or coordinate releases through explicit links.
A Release can involve several Products and Epics; partial artifact inclusion is
not proof that a complete Story shipped. The Scorecard traces each Product's
investment to its own work without counting every Initiative Story for both.

Reuse the tools' identifiers within their source and tenant/project scope. Preserve aliases when source labels change, and keep dated parent links when stories move between epics. Missing links remain visible as evidence gaps. A shared release ID supports a business-outcome comparison; it does not by itself prove causation.

Product views show sustained delivery investment and outcomes; initiative views compare a funded change with its targets; story/task views expose the evidence. The scorecard still uses the distinct story, deployment and team-period denominators defined in section 6.1.1, including spend and effort on unfinished work. Product/initiative views must disclose their attribution coverage and allocation rules. Overlapping views cannot be added together as independent benefits.

For the proposed fields and ID sources, see [schema section 3](lineage-schema.md#3-entities-and-fields). See [section 4](lineage-schema.md#4-relationships) for relationships, [section 6](lineage-schema.md#6-data-checks) for validation and [section 7](lineage-schema.md#7-evidence-for-the-six-scorecard-metrics) for the metric-to-evidence mapping.

### 5.4 How the IDs get attached at each stage

| | Stage 1: Story level (desktop) | Stage 2: Epic level (cloud agents) | Stage 3: Release level |
|---|---|---|---|
| Product / Initiative IDs | Resolve from the product catalog and dated tracker mappings; initiative is optional | Carry the mapped context into dispatch and retain the mapping version | Resolve release contents back to the dated story/product links |
| Story ID | Branch name, enforced by a branch rule and PR template | The story starts the agent run, so it's always there | Carried from the commits |
| Task ID | One per ADP session (laptop or cloud desktop) | One per agent run (ADP Task API) | One per nightly or pipeline run |
| Release ID | Read from the designated release registry or governed repository release record | Same authority; included in the release manifest | Propagated by the pipeline at promotion; each execution also has its own Deployment ID |
| Typical linked rate (illustrative) | 85–95% | ~99% | ~100% |

This is one of the main messages of the talk: **the more of the work the system starts, the better the data gets**, without anyone filling in anything.

### 5.5 Recommended systems of record for a typical enterprise

These are recommended ownership choices for onboarding, not a claim about a customer's current configuration or the availability of connectors. Select one authoritative owner for each ID type within the relevant scope. Different business units can use different tools when their namespaces and ownership are explicit.

| ID | Recommended system of record | Typical enterprise examples / rule |
|---|---|---|
| **Portfolio ID** | Product/portfolio catalog or governed direct Context entry | Stable collection identity; owns dated Product membership. No new execution scope or automatic financial authority. |
| **Product ID** | Enterprise product/service catalog or governed Profile registry | ServiceNow product/service portfolio or an established catalog. Map imported identities explicitly; ADP mints an owned Product identity when no external catalog owns it. |
| **Initiative ID** | Portfolio authority or governed direct entry | ServiceNow SPM, Planview, SAP EPPM or Jira Align. Owns the cross-product objective, dates and status; a source's delivery-project ID maps here only under an explicit rule. |
| **Contribution ID** | Context/portfolio authority | One agreed Initiative × Product × Project commitment; links source-native work and preserves dated revisions. |
| **Business Case ID** | Investment approval system | Existing portfolio/finance approval system. The AI Business Case Registry references its ID and maintains AI-specific baselines and targets. It can own the ID where no existing system does. |
| **Funding code / WBS ID** | Finance ERP | SAP or Oracle. Owns financial allocation and posted actuals. |
| **Epic ID** | Engineering work tracker | Jira, Azure Boards or the selected equivalent. |
| **Story ID** | Engineering work tracker | Same authoritative backlog as the epic. Branch names and PRs carry references to this ID. |
| **Task / agent run ID** | Execution platform | ADP Task API or another agent orchestrator. Instrumented desktop sessions receive an execution ID. A Jira sub-task remains a separate planning record. |
| **Model call ID** | AI gateway or invocation recorder | ADP gateway or equivalent. Retain the model provider's request ID as a linked identifier. |
| **Commit SHA / PR ID** | Source-code platform | GitHub, GitLab or Azure Repos, qualified by repository. |
| **Scenario ID** | Acceptance/test-management system | Xray, Zephyr or an explicitly maintained acceptance catalog in Git. |
| **Test ID** | Test-definition source | Version-controlled test code or a test-management catalog, with mappings between automated tests and managed test cases. |
| **Build / test-run ID** | CI and test execution system | GitHub Actions, Jenkins, Azure Pipelines or equivalent. Preserve jobs, attempts and retries. |
| **Release ID** | Designated release registry | Enterprise release management or a governed repository release record. Owns the release identity and manifest. |
| **Deployment ID** | Deployment execution system | Argo CD, AWS CodeDeploy or the delivery pipeline. A release can have multiple deployments. |
| **Incident ID** | Designated incident-management system | ServiceNow ITSM or Jira Service Management; link PagerDuty alerts where applicable. |
| **Governed Project ID** | Canonical ADP Project registry | Insights enforces this evidence/access scope; it does not mint a second Project ID. Source tracker/PMO project identifiers remain distinct mappings. |
| **Tenant ID** | ADP tenant/organization authority | Mapped to enterprise identity providers through approved onboarding. |

**Ownership rule:** source systems own their native records, IDs and business
relationships. Engineering Insights owns ingestion mappings, evidence provenance
and derived measurements, consuming the approved source-authority mappings. Profile
owns its Product/Project bindings; Context/portfolio owns Contributions and work
attribution; trackers and release systems own their respective facts. If Insights
creates an internal ingestion ID, retain the source-native ID alongside it.

The [Profile/Context integration contract](lineage-schema.md#52-profile-and-context-integration-contract)
defines authorized change/revision reads, independent relationship and mapping
versions, and the remaining registry-adapter admission work. Saving a simple
Product does not require an Initiative, Release or operational Insights adapter.

Record the business owner and conflict-resolution rule for each mapping. For example, if a branch name and Jira disagree about a story's epic, the work tracker's dated relationship takes precedence and the discrepancy remains visible. The AI Business Case Registry owns AI measurement targets; ERP owns posted financial actuals. A release approval or change ticket references the actual deployment record. See [schema section 3.11](lineage-schema.md#311-system-of-record-configuration) for the configuration and ownership checks.

---

## 6. What it measures

The metrics are in four layers. The core set is what the audience sees at every stage. The rest is detail that explains movement in the core set.

### 6.1 Layer 1: The six outcome metrics in the AI ROI Scorecard

**Use these six rows at every stage.** Retain the existing five metrics and add human effort per shipped story to make the capacity benefit of autonomy explicit. These measure outcomes for the organization adopting agentic engineering: delivery speed, output, production quality, cost and human capacity. Customer adoption, conversion and revenue remain external business outcome feeds.

#### 6.1.1 Exact definitions and collection methods

| ID and scorecard metric | Exact calculation | Evidence and collection | Customer benefit and direction |
|---|---|---|---|
| **EI-01 · Lead time per story** | Median elapsed calendar days from the earliest commit linked to a story to its first successful production deployment containing all changes required by its accepted completion criteria. Include review and deployment waits. Select stories by their first production deployment date. | GitHub commit and PR events, story acceptance state from Jira or Linear, and the deployment manifest linking commits to production. | **Lower:** customers receive completed features and planned improvements sooner. |
| **EI-02 · Stories shipped per team per week** | Count distinct stories first completed in production in the reporting period, divided by the number of calendar weeks in that period, for one named team. Count each story once. Exclude cancelled work and repeated deployments of the same story. Report bug-fix work separately from planned feature throughput. | Story IDs and type from Jira or Linear, accepted PRs, deployment manifests and dated team ownership. | **Higher:** the organization delivers more planned work with comparable team capacity. |
| **EI-03 · Change failure rate** | 100 × distinct production deployments that cause a rollback, emergency corrective deployment or service incident within 7 days ÷ all eligible production deployments. Count a deployment once even if it causes several failures. Include only deployments whose full 7-day observation window has elapsed. | Deployment IDs, rollback and hotfix events, and incident records with a confirmed causal link to the deployment. An unrelated incident within 7 days does not count. | **Lower, or no worse during initial adoption:** fewer releases disrupt customers or require emergency repair. |
| **EI-04 · Rework rate** | 100 × distinct shipped stories requiring a corrective fix or revert within 30 days of first production deployment ÷ all eligible shipped stories. Count each story once and include only stories with a complete 30-day observation window. Exclude new scope and planned enhancements. | Corrective PR or revert linked to the originating story and commit, with a correction reason in the work record. | **Lower:** more delivered work stays delivered, freeing capacity for the next customer need. |
| **EI-05 · Total cost per story** | (All in-scope AI and delivery platform costs incurred by the team in the period + estimated human delivery hours in that period × the agreed loaded hourly rate) ÷ distinct stories first shipped by that team in the same period. This is the team's delivery cost per shipped story. Include spend and effort on retries, abandoned work, corrective work and work still in progress. | Reconciled cost from the Budget Controller, finance-approved allocation of subscriptions and shared infrastructure, human effort evidence from EI-06, and shipped-story counts. | **Lower:** less delivery cost for each unit of completed work. Keep AI spend and estimated labor cost visible as separate components. |
| **EI-06 · Human effort per shipped story** | Estimated active human delivery hours for the team in the period ÷ distinct stories first shipped in the same period. Include planning, implementation, prompting, supervision, review, validation and corrective work. Include effort on failed or unfinished work. Exclude unattended agent runtime and idle time. | Approved work-session instrumentation and active review intervals linked to work IDs, calibrated against an agreed activity sample. Deduplicate overlapping intervals for each person. Label unobserved activities and estimate coverage explicitly. | **Lower, with quality holding:** fewer human hours are required per shipped story, creating capacity for design, customer work and more complex engineering. |

**Measurement rules shared by all six:**

- **Reporting period:** refresh daily, review a rolling 28-day window, and assess against the business case monthly. EI-02 divides a 28-day count by four. For EI-03, use deployments in the 28-day cohort ending 7 days before the report date. For EI-04, use shipped stories in the 28-day cohort ending 30 days before it. Display cohort dates and observation lag beside the values. Recent cohorts remain provisional until their quality windows mature.
- **Correct measurement level:** EI-01 and EI-04 use story cohorts; EI-03 uses deployments; EI-02, EI-05 and EI-06 use team-period totals. Aggregate by recomputing from underlying records. Do not average team percentages, medians or unit costs without their underlying populations.
- **A stable unit of work:** keep story completion criteria and classification fixed. Split comparisons by work type, agreed size band and risk class, then compare like with like. Display team staffing alongside throughput. More staff or smaller stories can raise throughput without an autonomy benefit. A partial deployment does not count as a shipped story.
- **One output denominator:** for EI-01, EI-02, EI-04, EI-05 and EI-06, an eligible story is a pre-agreed backlog item for a new capability or planned engineering improvement. Corrective fixes are tracked as rework against the original story; standalone defect fixes are shown separately. Their cost and human effort remain in the team's delivery totals. Keep this classification fixed so generating more repair work cannot inflate planned output or reduce reported unit cost.
- **Visible unfinished work:** show work in progress, ageing stories and abandoned work alongside the scorecard. A median over shipped stories can hide work that never finishes. Period cost includes that work; the story drill-down separately shows directly attributed lifetime cost, so its average need not equal EI-05.
- **Cost coverage:** allocate shared platform cost once using a documented rule. Reconcile AWS spend to the relevant AWS bill and non-AWS subscriptions to their own invoices. Baseline coding-assistant and platform costs remain in scope. Do not add metered usage costs to the same billed costs a second time.
- **Human effort is estimated:** session elapsed time and PR open-to-merge time are not active labor. Count one person's concurrent intervals once, allocate them across work, and retain the method version. If only coding and review are observable, show that limited scope and withhold a full human-effort or total-cost verdict until the missing effort is estimated credibly. Baseline and later stages must use the same scope and method. Report team aggregates.
- **No double counting of benefits:** EI-06 supplies the labor component of EI-05. Do not add their monetary benefits together. Released capacity becomes cash savings only when finance verifies an actual reduction or avoided expense; otherwise report it as capacity available for other work.
- **Evidence before attribution:** show sample counts, coverage and uncertainty with comparisons. Compare rollout teams with comparable teams still waiting, accounting for baseline differences and changes in work mix. A before/after improvement alone does not prove that increased autonomy caused it. An empty eligible cohort has no median or rate; a zero denominator has no unit cost or effort value. Display **Not enough data**, while retaining total spend and work in progress.

#### 6.1.2 How customer outcomes improve across Story, Epic and Release

The progression has three stages: **Story, Epic and Release**. Each stage delegates a larger scope of work to agents and addresses another source of delay or human effort. Throughput should rise; lead time, cost and human effort per shipped story should fall. Change failure and rework should stay within the quality limits and improve as the controls mature. These are expected benefits to test against the customer's baseline, rather than guaranteed gains from adopting more autonomy.

**Stage 1 · Story: help a developer complete a story faster.** A developer directs agents to implement changes and write tests, then reviews and accepts the work. The first customer benefit is shorter implementation time and less hands-on effort per story. This can reduce total cost per story even as AI spend rises. Review and release queues still constrain how much reaches production. Faster generation can also create more corrections, so the scorecard checks whether throughput gains survive the rework window.

**Stage 2 · Epic: coordinate delivery across multiple stories.** Workflows and cloud agents execute independent stories in parallel, carry context between tasks and continue while people are away. People define acceptance criteria, resolve dependencies and review the results. The customer can deliver more of an epic with the same team capacity, with less repeated setup and supervision. Story budgets based on Stage 1 cost history, model routing and quality gates aim to reduce wasted runs and corrections. The next constraint is often review capacity: more agent output improves throughput only when the team can validate and ship it.

**Stage 3 · Release: move accepted work through to production.** Automated validation, environment promotion and release orchestration reduce the coordination and waiting after implementation. People set policies, handle exceptions and approve high-risk changes, including payments changes where required. Customers receive completed work sooner, with less human release effort. Production-quality feedback can reduce recurring failures and rework. Canary checks and rollback also limit disruption, but a rollback still counts as a failed change. Added runtime and platform costs remain in the scorecard.

**The comparison stays consistent:** all six metrics retain the definitions in section 6.1.1. Story, Epic and Release describe the scope of delegated work and the drill-down available to the customer. Continue comparing cost and human effort per shipped story across stages; add epic and release totals as supporting views. This avoids making a larger unit of work look more expensive simply because it contains more stories.

| Scorecard metric and desired direction | Stage 1 · Story | Stage 2 · Epic | Stage 3 · Release |
|---|---|---|---|
| **Lead time per story · lower** | Shorter coding and test-writing time. Review and deployment waits remain. | Less waiting between tasks and stories as agents run asynchronously and workflows coordinate handoffs. | Less waiting for validation, environment promotion and an available release operator. |
| **Stories shipped per team per week · higher** | Developers complete more routine stories with assistance, subject to review capacity. | Parallel work across an epic increases accepted output when dependencies and review queues are managed. | Accepted work reaches production more continuously, increasing shipped output rather than accumulating in a release queue. |
| **Change failure rate · lower or no worse** | Preserve baseline quality through human review and existing tests. | Required tests, security checks and quality gates prevent more weak changes from reaching production. | Cross-service validation and staged promotion aim to reduce failures; production feedback improves subsequent releases. Rollbacks remain counted. |
| **Rework rate · lower** | May initially rise if faster generation outpaces clear requirements and review. Treat this as a quality issue. | Acceptance scenarios, shared context and feedback from corrections aim to restore or improve on baseline quality. | Release-level checks and production feedback aim to prevent recurring defects across stories and releases. |
| **Total cost per story · lower** | Reduced human implementation effort can outweigh new AI spend. | Better model routing, context reuse, fewer retries and history-based story budgets can lower cost further. | Reduced coordination and release effort can lower unit cost further if it exceeds added runtime, platform and oversight costs. |
| **Human effort per shipped story · lower** | Less manual coding and test writing; people still prompt, supervise and review each story. | Less repeated setup and task-by-task supervision; people specify work, review results and handle exceptions across an epic. | Less manual validation, promotion and release coordination; policy ownership, exception handling and required approvals remain. |

#### 6.1.3 Illustrative customer scorecard across the stages

**Illustrative worked example for the talk. These are proposed scenario values, not measured customer results, industry benchmarks or a forecast.** Agree customer-specific targets after recording the baseline. Keep the existing five-metric narrative and add the sixth row below.

| Metric | Baseline | Stage 1 · Story | Stage 2 · Epic | Stage 3 · Release | Release-stage change vs baseline |
|---|---:|---:|---:|---:|---|
| Lead time per story, median | 4.0 days | 2.8 days | 1.6 days | 0.9 days | 77.5% lower |
| Stories shipped per team per week | 6 | 8 | 14 | 20 | 3.33× the throughput |
| Change failure rate, 7-day window | 15% | 15% | 14% | 11% | 4 percentage points lower |
| Rework rate, 30-day window | 8% | 9% | 8% | 6% | 2 percentage points lower |
| Total cost per story, including estimated labor | $1,080 | $820 | $560 | $390 | 63.9% lower |
| Human effort per shipped story, estimated | 10.0 hours | 7.0 hours | 4.0 hours | 2.0 hours | 80% lower |

**The cost example reconciles:** at an illustrative loaded rate of $100 per human hour, labor contributes $1,000 / $700 / $400 / $200 per shipped story. AI and allocated delivery platform costs contribute $80 / $120 / $160 / $190. Their sums are $1,080 / $820 / $560 / $390. This makes the intended economics explicit: higher AI spend can coexist with lower combined delivery cost. The baseline already includes autocomplete and platform costs.

Lower unit cost does not imply a smaller total budget. At the illustrated output rates, weekly delivery cost rises from 6 × $1,080 = **$6,480** to 20 × $390 = **$7,800**, while weekly shipped output rises from 6 to 20. Finance decides whether the additional output justifies the total spend; the scorecard shows both volume and unit cost.

**The customer story in the example:** at **Story**, lead time falls to 2.8 days and human effort to 7 hours per shipped story, but rework rises to 9%, so quality needs attention. At **Epic**, coordinated agent work increases throughput to 14 stories per team per week while rework returns to the 8% baseline. At **Release**, reduced release waiting and coordination bring lead time to 0.9 days and effort to 2 hours, while the failure and rework rates improve to 11% and 6%. The customer gets more completed work with less effort per story and fewer corrections. These scenario values remain hypotheses to validate. A throughput increase, a cost reduction and fewer human hours are related views of the same delivery system, not three benefits to add together.

#### 6.1.4 Targets, evidence and scorecard verdicts

Store a target and review date for each metric in the Business Case Registry before assessing a rollout. Each scorecard row shows **baseline, target, current value, change, cohort dates, sample count, evidence status and verdict**. The baseline and current value use identical definitions. Targets are for the investment and the team system, never individual performance quotas.

The following Stage 1 example preserves the existing talk's targets and adds a proposed human-effort target. Later stage targets must be agreed separately; the trajectory above supplies an illustrative scenario for that discussion.

| Metric | Illustrative Stage 1 target | Illustrative Stage 1 value | Numerical assessment, assuming sufficient evidence |
|---|---|---|---|
| Lead time per story | At least 30% lower: ≤2.8 days | 2.8 days | Meets target |
| Stories shipped per team per week | At least 50% higher: ≥9 | 8 | Improving; target not yet met |
| Change failure rate | No worse than baseline: ≤15% | 15% | At the quality limit; requires adequate sample evidence |
| Rework rate | No worse than baseline: ≤8% | 9% | Quality guardrail breached |
| Total cost per story | At least 25% lower: ≤$810 | $820 | Improving; target not yet met |
| Human effort per shipped story | At least 30% lower: ≤7.0 hours | 7.0 hours | Meets target if effort coverage is sufficient |

**Evidence status comes before a verdict.** The 92% linking example in the talk is below the proposed 95% threshold. Affected story-based estimates may appear as **Provisional**, with the missing share visible, but do not receive a firm "target met" verdict or support an autonomy promotion. A metric with independently complete inputs, such as deployment failure rate, is assessed against its own required evidence checks. Use **Not enough data** when a denominator, observation window or essential source is unavailable. See section 6.4.

**Promotion rule:** a speed, throughput, cost or effort improvement supports more autonomy only when change failure and rework remain within the approved limits, evidence is sufficient, and the required security and release gates pass for that work's risk class. Define the review duration, minimum sample and acceptable uncertainty in the Registry before rollout. Fewer mandatory reviews are not a success metric by themselves.

### 6.2 Layer 2: Detail metrics (explain why the core moved)

**Cost detail**

| Metric | What it tells you |
|---|---|
| AI cost per story, epic, release | The AI part of cost per story, on its own |
| Cost by serving path (Bedrock vs self-hosted on HyperPod) | Whether routing to cheaper models is paying off |
| Cost per accepted change | Cost divided by changes that were merged and not reworked. Catches "cheaper per token but fails more". |
| Tokens per task, and tokens per task when resuming | Whether memory and the knowledge graph cut how much context is resent |
| Cache read share | How much context is reused instead of re-sent |
| Fixed context per call (system prompt and tool descriptions) | The baseline cost every call pays |
| Spend on failed or abandoned runs | Cost with no output |
| Runs stopped by a limit (per-run limit, daily cap) | How often the controls prevented a runaway |
| Desktop and agent compute per story | Non-token cost, from the cloud desktop and agent runtimes |

**Flow detail**

| Metric | What it tells you |
|---|---|
| Lead time broken into stages: coding → waiting for review → in review → waiting to deploy | Where time actually goes. In Stage 2, review usually becomes the bottleneck. |
| Review time per PR, and review queue length | Whether people can keep up with agent output |
| Work in progress per team | Whether parallel agents are creating more half-finished work |
| Burndown of work and AI spend for each epic, side by side | Whether spend is running ahead of progress. Forecast to finish = spend so far ÷ share of work done. |

**Quality detail**

| Metric | What it tells you |
|---|---|
| First-time gate pass rate (tests, coverage, security, review) | How often agent work is right first time |
| Test pass rate and flaky test rate | Test health, so failures can be trusted |
| Coverage change per PR | Whether agent work is tested as well as human work |
| Security findings introduced, and time to fix | Whether agents add risk |
| Reverts per release | A fast signal before change failure rate catches up |
| Scenarios covered per story | Whether acceptance criteria are actually tested |

### 6.3 Layer 3: Agent-specific metrics (decide what agents are trusted with)

Every core and detail metric can be split by **who did the work** (human session, cloud agent, workflow step, nightly agent) and by **cohort** (rollout wave). On top of that:

| Metric | What it tells you | Used for |
|---|---|---|
| Agent share of shipped stories | How much of the delivery agents now do | Context for every other number |
| Agent vs human on lead time, rework, change failure rate, findings | Whether agent work holds up | Deciding where agents can work with less review |
| Retries per story | How often agents go round in circles | Spotting unclear stories or weak prompts |
| Steer, pause and abort rate | How often people have to intervene | Readiness for more autonomy |
| Memory effect: first-time pass rate and retries over time, for similar story types | Whether agents improve with experience | Showing the value of episodic memory |
| Autonomy readiness by risk class (e.g. UI, tests, payments core) | Whether agent work in an area has matched human work for a set period | Stage 3 decisions on what can merge or ship without a person |

### 6.4 Layer 4: Data trust (shown next to every number)

| Check | Definition | Example threshold |
|---|---|---|
| Linked | Events with a valid story ID ÷ all events | 95% or more |
| Spend captured | Cost represented in the reconciled ledger ÷ independently billed cost for the same scope and period; check AWS and non-AWS sources separately | At least 98% captured, with reconciliation variance explained |
| Tests reported | Merged PRs with test results attached ÷ merged PRs | 95% or more |
| Orphans | Records missing a required or expected parent; deliberately omitted optional Epic/Initiative links excluded | 2% or less |
| Human effort covered | Share of in-scope delivery activity represented by the agreed effort estimate, checked against the calibration sample | Coverage and sampling uncertainty approved before a cost or capacity verdict |
| Fresh | Time since the latest successful source sync or heartbeat, excluding the separately disclosed billing lag | Under 1 hour for operational event sources |

**Rule:** apply checks to the inputs each metric actually needs. Show **Provisional** estimates with their coverage and missing share when completeness is below the approved threshold; withhold firm success verdicts and autonomy promotion based on those estimates. Show **Not enough data** when essential sources, denominators or complete observation windows are unavailable. A current snapshot can be provisional while an older, mature cohort has an assessed result. Never interpret no reported incidents or no reported effort as zero without checking source coverage. This makes the 92% linking example explicit without presenting it as proven ROI.

### 6.5 What it deliberately doesn't measure

- **Token leaderboards or tokens per developer as a goal.** They reward using more AI, not delivering more.
- **Lines of code or PR counts on their own.** Both rise with agents regardless of value.
- **Individual developer scores.** Data is at team level by default, with restricted access to anything individual.
- **Business outcomes.** These arrive from product analytics at the boundary.

---

## 7. How the metrics support AI ROI

### 7.1 From business case to scorecard

The business case says what the AI is meant to deliver. Engineering Insights supplies the evidence.

| Business case says | Engineering Insights supplies |
|---|---|
| Goal: ship features faster without lowering quality and release human capacity | Lead time, throughput, change failure rate, rework and human effort per shipped story |
| Cost you'll allow | Total cost per story and AI cost per story, against the budget |
| Unit of work: story | Story-linked evidence, with story, deployment and team-period denominators as defined in section 6.1 |
| Boundary: software delivery and shipped-change quality | Production delivery plus 7-day failure and 30-day rework windows; release ID shared with product analytics |
| Review dates | Monthly results against targets, with evidence status preceding the performance verdict |

The **AI ROI Scorecard** puts these side by side for the project. The **AI ROI Lake** puts every project's Scorecard side by side, comparing each against its own targets rather than averaging ROI across different kinds of work.

### 7.2 Feeding back into Control

Engineering Insights doesn't only report. Its results change how the work is controlled.

| Feeds back to | What it provides | Effect |
|---|---|---|
| AI Budget Controller | Actual cost of similar stories, by size and type | Story budgets in Stage 2 are set from history, not guessed |
| Quality gates | Gate results and rework by agent and story type | Gates tightened where agent work is weaker |
| Autonomy decisions (Stage 3) | Agent vs human results by risk class over time | Agents get more freedom only where the data shows it's earned |
| Model routing | Cost per accepted change by serving path | Routing decisions based on quality-adjusted cost |
| Training loop (Stage 3) | Accepted, edited and rejected changes with test outcomes | Labelled data for fine-tuning |

**Reverse ETL is a proposed outbound path, not a deployed integration.** The
[delivery architecture](architecture.md) places publishers after the evidence
store, metric engine and data-trust checks. They write selected results back
through the project's registered source connections, not by changing tracker
or engine records in place:

| Destination | Proposed evidence-enriched fields | Ownership boundary |
|---|---|---|
| Work trackers (Jira, Linear, GitHub Issues/Projects) | Story actual lead time, cost-to-date, rework state and evidence link; governed epic roll-ups | Tracker remains the system of record for human-entered intent, estimates and status |
| Project-management and PMO/portfolio tools | Initiative ROI, delivery forecast against the business case, evidence and data-trust state | Portfolio owner keeps authority over approved plans and targets |
| ADP Control | Historical cost for budget decisions, quality signals for gates, and agent-versus-human evidence by `risk_class` for autonomy and routing | Budget Controller and policy/gate owners decide and enforce; Insights only supplies evidence |

Each publisher requires an explicit grant for the registered connection and a
recorded policy listing the allowed destination fields. Write-back is
**field-scoped, policy-gated, idempotent and reversible**, versioned by
`mapping_version` and audited with its source evidence and data-trust state.
Never overwrite a human-entered field without a recorded policy; a field with
no policy is refused. A retry must not create duplicate updates, and a revoked
grant or disconnected source stops new writes. Values below the data-trust
floor are not published as authoritative metrics. EI-E1 owns this later-wave
target; its W4 publisher delivery story has not yet been assigned an ID.

### 7.3 Answering the CFO at each stage

| End of | What Engineering Insights lets you say |
|---|---|
| Story | What delivery costs per shipped story, how speed and human effort changed from baseline, and whether early quality or evidence gaps need attention |
| Epic | Whether agent workflows increase comparable output and reduce human effort and unit cost while meeting the quality guardrails |
| Release | Whether release automation sustains those gains within approved risk classes and, with the product analytics feed, whether the features delivered business results |

---

## 8. How it grows across the three stages

| | Stage 1: Story level | Stage 2: Epic level | Stage 3: Release level |
|---|---|---|---|
| New sources | GitHub, CI, Jira, deployment manifests, rollback and incident records, approved human effort estimates and baseline cost records; then ADP gateway costs and sessions | Agent runs, delivery engine events, gate results, pause/steer/abort actions, memory retrievals, serving path | Nightly runs, environment-promotion and canary detail, evals and product analytics feed; extend existing deployment and incident collection |
| New breakdown | By team, rollout wave, work type and risk; individual access restricted | By epic and recorded human/agent contribution | By release and eligible autonomy class |
| New detail shown | All six scorecard metrics, cache reuse and data trust | Burndowns, review queue, memory effect, cost per accepted change | Time to recover, cost per release, autonomy readiness |
| Feeds back to | Budget Controller (cost history) | Gates; story budgets | Autonomy decisions; training loop |

The six scorecard metrics stay the same throughout. Only the detail and the breakdowns grow. Record whether each work item uses Story, Epic or Release-stage automation, including its approval requirements. A team can use different stages for different work types. Keep mixed human/agent work as an explicit category rather than forcing a binary comparison.

---

## 9. Keeping the data trustworthy

1. **Capture work events automatically.** IDs come from branch names, the gateway, the ADP Task API, commit trailers and the pipeline. Routine reporting needs no timesheets or repeated manual tags. Human effort remains an estimate calibrated against an agreed activity sample; event collection alone cannot establish all human working time.
2. **Check every event when it arrives.** Missing fields or unknown IDs go to quarantine with a reason; nothing is silently dropped.
3. **Define each metric once, in code, with a version.** If a definition changes, history is recalculated from the evidence store so comparisons stay fair.
4. **Check against independent sources daily.** Cost against the AWS bill, deployments against the pipeline's history, merged PRs against Jira status.
5. **Make every number traceable.** Any figure can be clicked through to the test runs, scans, reviews and calls behind it.
6. **Show completeness next to the number.** The data trust panel distinguishes provisional estimates from assessed results and missing data, using the rules in section 6.4.

Known weak points and how to handle them:
- **Squash merges can remove commit trailers.** Fall back to PR metadata and count trailer loss in the linked check.
- **Developers forget branch naming on the desktop.** Branch rules and PR templates help; Stage 2 removes the problem because the story starts the run.
- **Work done outside the gateway** (personal subscriptions). An AWS-only reconciliation cannot detect all of it. Compare approved subscription invoices and a tool-usage inventory with the cost ledger; investigate unlinked commits and disclose remaining coverage gaps.

---

## 10. Privacy, trust and avoiding gaming

- **Team level by default.** Individual-level data has restricted access, and in the EU needs works council agreement. German law treats any system capable of monitoring employee performance as subject to co-determination.
- **Trends, not targets.** Metrics are shown as movement over time against the baseline, not as fixed targets for people. Once a number becomes a target, people work to the number.
- **Every speed metric has a quality partner.** Faster lead time doesn't count if rework or change failure rate rises.
- **Agent transcripts are stored separately** with tighter access; run records only point to them.
- **Personal context stays personal.** The knowledge graph and preferences on the cloud desktop are not sent to Engineering Insights.

---

## 11. Build on AWS, or reuse what you have

### 11.1 AWS services for each component

| Component | AWS services |
|---|---|
| Collector | Lambda function URL (webhook endpoint), Amazon EventBridge, Amazon Data Firehose, Amazon S3; EventBridge Scheduler for the nightly job |
| Linker | AWS Lambda with Amazon DynamoDB lookup tables (story → epic, task → story) |
| Evidence store | Amazon S3 for raw reports (JUnit XML, SARIF, coverage files); Amazon S3 Tables (Apache Iceberg, append-only, time travel) for parsed records |
| Work records | Amazon DynamoDB for current state; S3 Tables for history |
| Metric engine | Amazon Athena views; AWS Glue jobs |
| Access control | AWS Lake Formation (team-level default) |
| Dashboards | Amazon Quick Sight |
| Cost input | AI Budget Controller, reconciled with CUR 2.0 (AWS Data Exports) |

### 11.2 What ADP already provides

ADP already supplies several inputs: the gateway (with per-call cost and team attribution), billing to each team's AWS account, GitHub sign-in identity, ADP sessions, and the Task API with streamed progress events for agent runs. Engineering Insights builds on these rather than duplicating them. ADP does not currently provide the outcome tracker, metric pack, baselines, scorecard or lake.

### 11.3 Reuse existing tools where they fit

Developer insight platforms such as Jellyfish and Faros AI, and the open-source Apache DevLake, already collect delivery data and calculate DORA-style metrics, and some already link AI use to PRs. If a customer has one:
- **Keep it** for delivery data collection and the standard metrics.
- **Add what's new:** the shared story and task IDs stamped at the gateway and on every result, the join to reconciled cost from the Budget Controller, and the data trust checks. No existing product does these together today.

---

## 12. Rollout plan

| When | What | Outcome |
|---|---|---|
| Weeks 0–2 | Connect GitHub, CI, Jira, deployment and incident tools; backfill 3–6 months of available history; agree story eligibility, metric definitions, effort estimation and the loaded rate with finance | Establish available baselines for all six metrics. Start a prospective effort baseline if historical effort evidence is missing; do not invent it from PR elapsed time. |
| Weeks 2–4 | Connect the ADP gateway and Budget Controller; reconcile baseline subscriptions and platform cost; enforce branch naming; calibrate effort capture and start data trust checks | Cost and effort per shipped story with visible coverage; continue baseline collection until quality windows and sample requirements are satisfied |
| Weeks 4–8 | Onboard teams in waves; compare onboarded teams with teams still waiting | First real Stage 1 results against the baseline |
| Stage 2 | Add agent runs, gate results and delivery engine events; agent vs human comparisons | Story budgets set from history; gates tuned from data |
| Stage 3 | Extend pipeline and incident records with promotion, canary and eval detail; connect the product analytics feed | Release-level results; autonomy decisions by risk class |

---

## 13. Open questions

1. **Loaded rate for people's time:** agree one rate with finance and keep it fixed across stages.
2. **ADP Task API:** confirm it carries the story ID and that its run ID can be used as the task ID.
3. **Pause, steer, abort events:** confirm ADP emits these so they can be counted.
4. **Scenario tagging:** pick one approach per organization (Gherkin tags, a test management tool, or test annotations).
5. **Desktop compute cost:** decide how cloud desktop time is split across the tasks running on it (by active time per task is the default).
6. **Existing tools:** for each customer, decide whether Engineering Insights replaces or sits beside their current developer insights platform.
7. **Human effort coverage:** agree the active-time method, idle and overlap rules, treatment of planning and validation, and calibration sample. Record missing activity and use the same method before and after rollout.
8. **Scorecard contract:** approve the six targets and review dates, stable story classification, cost allocation, evidence thresholds, minimum samples and quality limits for each eligible risk class. Stage 1 measures story cost; work-level budget enforcement starts in Stage 2.

---

## 14. Worked scenario: business case to ROI scorecard

This scenario follows **checkout modernisation at a retailer**, tracing one story from investment approval to production evidence, then showing the pilot team's aggregate ROI scorecard. It uses one representative system for each role: ServiceNow, SAP, Jira/Xray, ADP, GitHub, GitHub Actions, Argo CD and product analytics. Equivalent enterprise tools can fill the same roles under the ownership rules in section 5.5.

**All IDs and results are illustrative.** The integrations describe the proposed architecture. They do not establish that these connectors or end-to-end capabilities are deployed. The example reuses the Story-stage targets and results in section 6.1.4.

### 14.1 The business approves the investment

The product owner proposes:

> Modernise checkout to improve customer completion rates. Use AI-assisted engineering to deliver the changes faster and with less human effort, while maintaining production quality.

The enterprise systems establish the business context:

| System | Record it owns | Example |
|---|---|---|
| ServiceNow product catalog | Product | `PRD-CHECKOUT` |
| ServiceNow SPM | Delivery initiative | `INIT-2026-014` — Checkout modernisation |
| ServiceNow investment approval workflow | Approved business case | `BC-2026-014` |
| SAP | Funding/WBS reference and posted financial actuals | `WBS-CHECKOUT-2026` |
| ADP tenant authority | Tenant, with approved enterprise identity mapping | `TENANT-RETAILER` |
| Canonical ADP Project registry | Persistent delivery/operating Project; Insights enforces evidence access using the same ID | `INS-PROJ-COMMERCE` |

Microsoft Entra ID supplies authenticated identities through the configured enterprise identity integration. ADP resolves their authorized tenant and project access. An Entra tenant or group identifier is mapped explicitly to ADP authority.

**ROI module: AI Business Case Registry.** The Registry references the approved business case and records the product, initiative, funding reference and accountable owner. It registers the delivery baseline, all six outcome targets, pilot cohort, comparison method, review date and evidence thresholds before evaluation. It also records the seven-day change-failure and thirty-day rework windows.

The business case has two evaluation questions: whether AI improves delivery economics and quality, and whether the resulting feature improves checkout completion. The Registry records the separate feature hypothesis and product experiment design. Both contribute evidence to the investment decision, with distinct attribution requirements.

### 14.2 The investment becomes engineering work

**Jira** holds the epic and stories:

```text
Scope: TENANT-RETAILER / INS-PROJ-COMMERCE

Product: PRD-CHECKOUT
  Initiative: INIT-2026-014
    Epic: CHK-12 — Checkout redesign
      Story: PROJ-123 — Add saved cards

Business case BC-2026-014 funds the initiative.
SAP WBS-CHECKOUT-2026 identifies its financial allocation.
```

**Xray** holds acceptance scenarios, including saving a card after a successful transaction, rejecting an expired card and preventing access to another customer's saved card. The story references those scenarios, and versioned mappings connect them to executable tests.

The lineage service resolves the story's product, initiative and funding context from the authoritative dated relationships. Jira remains the owner of the backlog relationships even when branch names and PRs carry copies. See [schema section 4](lineage-schema.md#4-relationships) for relationship and attribution rules.

### 14.3 Agents execute within approved spending limits

A developer starts an **ADP session** for `PROJ-123`. ADP records an execution task, such as `TSK-01`, and maps it to the source session or agent-run identity. The agent calls models through the **ADP gateway**, using **Amazon Bedrock**. If the deployment also uses models hosted on **SageMaker HyperPod**, each call records that serving path and its applicable usage/cost evidence.

**ROI module: AI Budget Controller.** The Controller:

1. Applies the approved team/user spending policy through supported gateway controls.
2. Attributes calls and runtime usage to the task and story where evidence supports the link.
3. Includes retries and abandoned attempts in the in-scope cost totals.
4. Reconciles usage with AWS billing and approved SAP/invoice allocations, counting each cost once.

At the **Story stage**, this supports spending controls and story-level attribution. Later **Epic-stage** orchestration can apply work-level budgets where enforcement is implemented. SAP owns posted financial actuals. The Controller owns the reconciled delivery-cost attribution, including the distinction between estimated usage cost and reconciled cost. Missing story links remain visible and do not remove spend from the team's totals.

### 14.4 Code becomes testable evidence

The task creates a **GitHub PR** referencing `PROJ-123`. Commit provenance links the code to the ADP task. **GitHub Actions** executes the build, automated tests, coverage and security checks.

| Evidence | Authoritative source | Lineage connection |
|---|---|---|
| Commit and PR | GitHub | Task ↔ Commit ↔ PR ↔ Story |
| Acceptance criterion and scenario | Jira and Xray | Story revision → Criterion revision ↔ Scenario revision |
| Executable test | Version-controlled test code | Scenario ↔ Test |
| Pack execution and test attempts | GitHub Actions and test reports | Run + Commit → Planned test instances → Results, preserving job/attempt identity |
| Review decision | GitHub | Review → PR |
| Scan findings and gate results | Scanner and configured gate evaluator | Finding/result → Commit, PR or deployment |

Failed attempts and retries remain visible. Human effort includes approved estimates for planning, prompting, implementation, supervision, review, validation and correction. Session duration alone cannot establish that effort. The evidence records its estimation method, coverage and overlap removal as described in [schema section 3.10](lineage-schema.md#310-evidence-for-human-effort-and-allocated-cost).

### 14.5 Accepted work reaches production

For this example, a **governed GitHub Release record** owns `REL-2026.11.2` and its manifest. **Argo CD**, through the configured deployment recorder, supplies distinct deployment records for staging, canary and production. The manifest and acceptance evidence identify which successful production deployment first completes `PROJ-123`.

**ServiceNow change management** records the required approval. The deployment record establishes what actually ran. If a production problem occurs, **PagerDuty** supplies the alert and **ServiceNow ITSM** owns the incident record. Incident review confirms any causal link to the deployment; temporal proximity alone is insufficient.

**Product analytics**, such as Amplitude, measures checkout completion for the released feature or experiment. Its results reach the scorecard through a scoped release/feature reference. The analytics system owns that business-outcome measurement.

### 14.6 Engineering Insights joins cost to outcomes

**ROI module: AI Outcome Tracker.** Engineering Insights connects the business case and product/initiative to the epic, story and task. From the task it traces calls and commits, quality evidence, the release, deployments and linked incidents. This is the lineage described in section 5.3, with the detailed records in the [lineage schema](lineage-schema.md).

The tracker combines those records with reconciled cost and approved human-effort evidence. It calculates the six delivery metrics using the definitions in section 6.1.1, checking evidence coverage, unresolved links, cost reconciliation, complete quality-observation windows and consistent cohorts. Each scorecard value retains its period, denominator, definition version and supporting evidence references.

**ROI module: AI ROI Lake.** The Lake retains versioned metric history, mappings and evidence references so the result can be reproduced and compared across initiatives. It receives evidence and metric updates throughout the workflow. Raw reports remain in the governed evidence store under their access and retention policies.

The proposed AWS implementation uses **Lambda/EventBridge** for collection, **DynamoDB** for current mappings, **S3/S3 Tables** for evidence and history, **Athena/Glue** for calculation and **Lake Formation** for governed analytical access. It retains the shared service's tenant/project authorization requirements. These services implement the measurement flow; the execution systems and gates continue to enforce approved actions.

### 14.7 The investment owner sees the ROI scorecard

**ROI module: AI ROI Scorecard.** The scorecard, surfaced through **Amazon QuickSight**, compares registered targets with the pilot's outcomes. The story above demonstrates traceability; the table below describes the eligible pilot-team populations rather than one story. Quality rows use mature cohorts with the observation lags specified in section 6.1.1.

**Illustrative Story-stage results, assuming sufficient evidence:**

| Outcome | Baseline | Target | Pilot | Assessment |
|---|---:|---:|---:|---|
| Lead time per story | 4.0 days | ≤2.8 days | 2.8 days | Meets target |
| Stories shipped per team per week | 6 | ≥9 | 8 | Below target |
| Change failure rate | 15% | ≤15% | 15% | At quality limit |
| Rework rate | 8% | ≤8% | 9% | Guardrail breached |
| Total cost per shipped story | $1,080 | ≤$810 | $820 | Lower, but below savings target |
| Human effort per shipped story | 10 hours | ≤7 hours | 7 hours | Meets target |

The cost calculation reconciles: **7 hours × $100 loaded rate + $120 AI/platform allocation = $820 per shipped story**. This is a team-period unit cost. Its numerator includes unfinished, abandoned and corrective work, so it need not equal the lifetime cost directly attributed to `PROJ-123`. Compared with the $1,080 baseline, the unit-cost reduction is approximately 24.1%.

Actual use of this scorecard requires cohort dates, sample counts, attribution and effort coverage, source freshness, cost-reconciliation status and uncertainty alongside the values. The example assumes those checks pass to illustrate the numerical verdicts; it supplies no observed customer dataset. If evidence falls short, apply the Provisional / Not enough data rules in section 6.4 before assigning success verdicts.

The investment summary shows:

| Investment question | Current conclusion in this example |
|---|---|
| Has delivery become more efficient? | Lead time and human effort improved; unit cost fell about 24%. |
| Is greater autonomy justified? | Hold promotion while rework exceeds the approved limit. |
| Has the feature improved checkout completion? | Await the product experiment's result. |
| Has AI produced a verified financial return? | Pending finance validation of realized benefits and full incremental program costs. |

Released capacity is reported separately from cash savings. The cost and effort improvements are related views of the same system and must not be added together as independent benefits. A feature's conversion uplift requires additional evidence before attributing it to AI. Any financial return calculation includes setup, training, integration and ongoing incremental costs once, and values supported sales benefits using attributable contribution margin. The scorecard therefore leaves financial ROI pending in this example.

### 14.8 The scorecard changes the next decision

The investment owner continues the controlled pilot, addresses rework and postpones increased autonomy. The Registry records the decision and review date. Approved policy changes flow to the Budget Controller and execution gates, and the next scorecard uses the same outcome definitions to assess the result.

An approved investment has become identifiable work, the work has generated cost and outcome evidence, and the scorecard now supports the enterprise's next funding and delegation decision.
