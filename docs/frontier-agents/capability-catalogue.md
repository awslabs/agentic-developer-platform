# AWS agent capability catalogue for ADP

Reviewed against AWS documentation, public CLI references and ADP spike evidence on **17 October 2026**. Prices are public USD rates, before account-specific credits, discounts and taxes. Preview terms can change.

The unit of enablement is a **capability**, not an entire agent. A Product profile supplies context and authorized connection references; ADP checks provider prerequisites before offering activation. Profile completeness alone does not establish permissions, provider eligibility or successful execution.

Related: [frontier-agent documentation](README.md) and the [Product Onboarding integration design](../product-context/aws-agent-capabilities.md).

## Capability table

Commands below are **provider CLI entry points**, with required arguments omitted for readability. They are not new ADP commands or complete copy-and-paste setup recipes. AWS CLI v2 must include the referenced service operations. Transform uses its separate `atx` CLI. Provider setup and result retrieval are described below.

| Agent | Capabilities offered | CLI commands / supported access | How the Product profile unlocks it | Expected pricing structure |
|---|---|---|---|---|
| **AWS DevOps Agent — operational investigation** | Investigate incidents, correlate telemetry and changes, explain likely causes, propose mitigation, answer operational questions. | `aws devops-agent create-chat`; `aws devops-agent send-message`; `aws devops-agent list-journal-records`. Event/schedule automation uses `create-trigger` after configuration. | Linked AWS environment, account/Region, scoped operational role, application scope, logs/metrics/alarms; deployment and incident-tool connections improve coverage. Mitigation execution additionally needs the relevant write permissions and authorization. | **$0.0083/agent-second** for active operational work, approximately **$0.498/agent-minute**. Idle/waiting time is not charged. Telemetry queries and other AWS services cost extra. [D-price] |
| **AWS DevOps Agent — code and operational context** | Read source snapshots, use runbooks and skills, retrieve versioned assets/memory. Connected environments can support topology and dependency discovery. | `aws devops-agent register-service`; `associate-service`; `list-assets`; `get-asset-content`; `get-asset-file`; ask questions through `create-chat` / `send-message`. Topology UI export is documented; a dedicated topology-export CLI has not been established. | Repositories and immutable revisions, or an ADP-produced S3 source snapshot with scoped read access; runbooks/docs; environments for live discovery. A readable S3 snapshot does **not** establish full native repository indexing or PR integration. | Operational reasoning uses the DevOps agent-second rate; S3, telemetry and connected-service costs are separate. No separate asset-read price is identified in the cited pricing page. [D-price] |
| **AWS DevOps Agent — release readiness** | Review changes against team standards, identify cross-repository risks and assess CloudFormation access controls. | `aws devops-agent create-chat` / `send-message` to request a review; retrieve executions/journals. Native task type `RELEASE_READINESS_REVIEW` is also exposed by `create-backlog-task`; creating a task alone is not evidence of a completed review. | Connected and indexed repositories, target branch/commit or PR/MR, dependency context, optional team standards/skills. Native repository authorization must be established separately from ADP's GitHub connection. | **No additional agent charge during release-management preview**; connected services still charge normally. [D-release] [D-price] |
| **AWS DevOps Agent — release testing** | Generate and execute change-specific tests against a deployed application; documented coverage includes APIs, UI/UX and integrations. | `aws devops-agent create-asset --asset-type test_profile` with the documented profile content/metadata; `create-chat` / `send-message`; `list-backlog-tasks`; `list-journal-records`. Native task type: `RELEASE_TESTING`. No `create-test-profile` command. | Test environment URL, deployed source revision/change, test intent, API specification where available, dedicated test-user secret reference and appropriate isolation. Private endpoints require network/runtime configuration. A public health URL alone is insufficient. | **No additional agent charge during preview**; test-environment compute, networking, secrets and other AWS services are additional. [D-assets] [D-release] [D-price] |
| **AWS Security Agent / Continuum — threat modeling** | Generate architecture/system overview, trust boundaries and STRIDE threats with recommendations. | `aws securityagent create-threat-model`; `start-threat-model-job`; `batch-get-threat-model-jobs`; `list-threats`; `batch-get-threats`. | Architecture/design documents, code, or both; application purpose and boundaries; Agent Space, service role and logging access. A deployed environment is not required for a document assessment. | **No charge for threat modeling while in preview**, subject to quotas; supporting AWS resources may charge. [S-overview] [S-price] |
| **AWS Security Agent / Continuum — code security** | Full-code security review, PR/MR review, organizational requirement checks and remediation proposals. | `aws securityagent create-code-review`; `start-code-review-job`; `list-findings`; `batch-get-findings`; `start-code-remediation`. PR/MR automation additionally uses repository integration. | Authorized repository or supported S3 source, revision/change scope and security requirements. Creating remediation PRs/MRs requires provider repository write access; code-read access alone does not unlock it. | **Code reviews are free during preview**, subject to quotas. No separate remediation unit price was established in this review; verify current terms before enabling it. [S-overview] [S-price] |
| **AWS Security Agent / Continuum — design security** | Review architecture documents against organizational security requirements before implementation. | Documented **web-application workflow**. A dedicated public design-review run command was not established; do not substitute threat-model commands and label them design review. | Design documents, application context, applicable security requirements and authorized Agent Space access. | **No charge during preview**, subject to review quotas. [S-overview] [S-price] |
| **AWS Security Agent / Continuum — penetration testing** | Test deployed applications, validate exploitable findings, revalidate fixes and propose code remediation. | `aws securityagent create-target-domain`; `verify-target-domain`; `create-pentest`; `start-pentest-job`; `list-findings`; `batch-get-findings`; `stop-pentest-job`. Revalidation uses `start-pentest-job --job-type REVALIDATION` with its required inputs. | Verified target domain, environment URL, explicit testing scope, test authentication secret references and reachable network path; optional code/docs improve context. Explicit authorization and a task-hour allowance are needed for active testing. | **$50/task-hour**, metered per second of cumulative task work. Parallel tasks can exceed wall-clock duration. First three revalidations per finding are free under current terms; trial allowances may apply. [S-pentest-price] |
| **AWS Well-Architected Agent — recommendations** | Prioritize resource/application improvements across cost, security, resilience and performance using business goals. | `aws wellarchitected create-agent-profile`; `create-agent-goal`; `create-agent-context`; `get-agent-profile`; `list-agent-recommendations`; `get-agent-recommendation`; `update-agent-recommendation-status`. | Accounts/Regions, application overview, goals/pillars, application scope/tags, execution role and workload access roles. **Business+ or higher Support** is required for the profile; monitored-account eligibility affects coverage. Cost Explorer improves cost precision. | Required Support subscription: Business Support+ is the greater of **$29/account/month** or tiered AWS-usage percentages, with a 30-day minimum. A separate WA Agent usage rate was **not established** from the reviewed sources; do not infer it from the older Well-Architected Tool's free pricing. Underlying services can charge. [W-start] [W-quotas] [Support-price] |
| **AWS Well-Architected Agent — architecture review** | On-demand IaC architecture recommendations aligned with selected goals and pillars. | `aws wellarchitected start-agent-recommendation-generation --types ARCHITECTURE`; `get-agent-recommendation-generation`; `list-agent-recommendations`. Required profile/scope/context arguments must also be supplied. | Eligible configured Agent Profile, goals and scoped IaC files/package or supported S3 input. Current quota: five architecture reviews per profile/day; input-size limits also apply. | Same Support prerequisite and pricing uncertainty as the preceding row; file storage and other supporting services are additional. [W-generation] [W-quotas] |
| **AWS FinOps Agent — costs and optimization** | Natural-language cost inquiry/forecasting, anomaly investigation, recurring reports, summaries of Cost Optimization Hub/Compute Optimizer recommendations, optional Jira/Slack delivery. | **Console/web application** for the agent workflow; no supported public `aws finops-agent` CLI or boto3 workflow was established. `aws ce get-cost-and-usage` accesses underlying billing data, **not the FinOps Agent**. | Scoped billing connection/Billing View, enabled Cost Explorer, product attribution through accounts/tags/cost categories, financial goals and agent/operator roles. Anomalies require Cost Anomaly Detection; optimization requires the relevant services enabled. Add report schedule, recipients and authorized integrations for delivery. Repositories are unnecessary. | **No agent charge during preview**, subject to a monthly usage limit. Cost Explorer/API calls and other connected services charge normally. No post-preview agent rate was found. [F-overview] [F-create] [F-setup] [F-faq] |
| **AWS Transform — custom modernization** | Analyze/refactor code, upgrade dependencies/runtimes, translate code and execute reusable transformation definitions. | `atx custom def list --json`; `atx custom def exec -n <definition> -p <checkout> -c <build-command> --limit <agent-minutes>`; `atx` for custom conversations. | Repository checkout/revision, language/runtime, desired change, build/test commands, writable scope, AWS identity and agent-minute budget. Native GitHub App reuse is unnecessary for a local checkout; publishing a PR remains separate. | **$0.035/agent-minute**, minimum billing increment one minute; active work only. AWS-managed `AWS/dotnet-modernization` includes **50,000 agent-minutes/month** at no charge under current terms. Builds/tests and retained infrastructure can cost extra. [T-cli] [T-price] |
| **AWS Transform — continuous modernization** | Analyze a repository portfolio, track findings and generate remediation branches/PRs/MRs. | `atx ct source add`; `atx ct analysis run`; `atx ct analysis get`; `atx ct remediation create`; `atx ct remediation status`. | Authorized repository collection or local source, selected analysis/transformation, build context and budget. Repository write credentials are needed for remote PR/MR creation. Security analysis additionally requires a configured AWS Security Agent. | Non-security analysis and remediation use Transform custom agent-minutes; Security analyses use Security Agent pricing. Execution infrastructure is additional. [T-continuous] [T-price] |
| **AWS Transform — specialist migrations** | Assessment, Windows/.NET modernization, VMware migration and mainframe modernization. | Workload-specific Transform workflows. This review does **not** establish a universal public CLI for all these agents; the custom `atx` command must not be presented as one. | Source workload inventory/artifacts, access to the relevant platform, migration/modernization target and workload-specific prerequisites. A generic repository/environment profile alone is insufficient. | Current Transform pricing lists assessment, Windows, VMware and mainframe agent capabilities at **no agent charge**. Target infrastructure, tooling and migration resources are charged separately. Not demonstrated by the ADP custom-refactor spike. [T-price] |

## Setup and results: what ADP must map

DevOps starts with `aws devops-agent create-agent-space`, service registration/association and the selected assets. Security starts with `aws securityagent create-agent-space`, applicable artifacts/integrations and runtime roles. Well-Architected uses the profile/goal/context commands above; its API does not create the IAM execution and workload access roles automatically. FinOps currently needs the documented console flow or a separately qualified browser adapter. Transform custom can operate on an authorized checkout without provisioning another provider Agent Space.

Each adapter fills the provider request from the Product profile, records the returned resource/job IDs, observes status, and imports supported results with source revision and timestamps. For AWS CLI operations, reviewed request files can be supplied with `--cli-input-json file://request.json`. Do not assume a common request schema across providers.

The Product profile should reference existing tenant-scoped connections and team membership. Reuse these input groups:

- **Basic context:** product purpose; repositories/revisions; architecture documents and runbooks; team standards.
- **Environments:** linked account/Region, application/resource scope, deployment revision, endpoints, telemetry and pipeline connections.
- **Testing/security:** test intent, dedicated test-user secret references, permitted target scope and security requirements.
- **Financials:** billing scope and attribution, goals/budgets, reporting preferences and an authorized execution allowance.

Connection grants, Support eligibility, enabled billing services and verified domains are **observed prerequisites**, not facts established by entering text in the profile. Secrets stay in ADP connections/vaults. Context prompts, tags and product names do not enforce tenant access boundaries. Preserve the existing **Only me / Product team** visibility and require an appropriately scoped provider resource before sharing its results.

Keep the UI simple: show each capability as **Ready to enable**, **Needs information**, **Needs account setup**, or **Enabled**. These are proposed display labels over the existing capability lifecycle, not a replacement state model. A useful hint names the missing input and outcome: “Connect test environment and test-user credentials to enable release testing.”

## Pricing interpretation

Expected cost = agent usage + required subscription + underlying AWS services − applicable credits. Support is an account subscription: count it once per subscribed account, not once per product or capability. A shared-account product needs an explicit allocation rule.

Illustrative list-rate calculations, excluding other services and discounts:

- Eight active DevOps agent-minutes: `8 × 60 × $0.0083 = $3.984` (about $3.98).
- Three Security pentest task-hours: `3 × $50 = $150`, regardless of whether those tasks execute concurrently.
- One hundred Transform custom agent-minutes: `100 × $0.035 = $3.50`.
- Business Support+ for four accounts: **at least $116/month**, potentially higher based on AWS usage. Tiers are 9% of the first $10,000, 7% of the next band to $80,000, 5% to $250,000, and 3% above that, calculated per account under current pricing.

DevOps currently offers a two-month trial with usage allowances. Paid Support customers may receive monthly DevOps credits based on the prior month's Support charge: 30% for Business Support+, 75% for Enterprise Support, 100% for Unified Operations. Security penetration testing currently offers eligible new customers a two-month trial with up to 400 task-hours per trial month. Treat trial eligibility and remaining allowances as account observations, not permanent catalogue discounts.

Provider quotas and AWS Budgets notifications are not interchangeable with an enforced per-run spending limit. Prefer documented execution limits where available, such as Transform's agent-minute limit and Security pentest `maxTaskHours`, alongside ADP's execution budget. Refresh preview terms and rates before activation.

## What ADP has demonstrated

The [consolidated spike evidence](https://github.com/aws-e/adp/issues/7919#issuecomment-6229885049) establishes:

| Capability | ADP evidence status |
|---|---|
| DevOps S3 source context | Live reads and snapshot refresh demonstrated. Some generated claims were inaccurate; persistent memory/topology and full native repository indexing were not proven. [PR #7978](https://github.com/aws-e/adp/pull/7978) |
| DevOps release management | Agent Space and test-profile lifecycle demonstrated. Native release testing did not run because the available fixture did not meet the required isolation constraints. [PR #7988](https://github.com/aws-e/adp/pull/7988) |
| Security threat modeling | Completed assessment of ADP architecture, 25 candidate threats and four source comparisons; this does not qualify code review, design review or penetration testing. [PR #7984](https://github.com/aws-e/adp/pull/7984) |
| Well-Architected | Preflight/eligibility checks only; Support prerequisite blocked the review. [PR #7985](https://github.com/aws-e/adp/pull/7985) |
| FinOps | Console setup and inquiry demonstrated; billing data was unavailable. Consolidated helper was not live-requalified and pending-inquiry recovery remains incomplete. [PR #7987](https://github.com/aws-e/adp/pull/7987) |
| Transform custom | Real provider-generated refactor and validation demonstrated; portfolio workflows and specialist migrations were not demonstrated. [PR #7991](https://github.com/aws-e/adp/pull/7991) |

Provider support in the main table is distinct from ADP implementation readiness. The spike resources were cleaned up; the catalogue does not describe currently retained, operational agents.

## Sources and maintenance

Use this document as the initial catalogue. Keep one maintained source for capability inputs, command mappings, pricing references and verification date; UI/CLI views can later consume a small structured version of the same records. Recheck the provider interface when upgrading the pinned CLI/SDK and refresh pricing before enabling paid work. Do not derive automation support solely from IAM action names.

- **D-CLI:** [DevOps Agent CLI operations](https://docs.aws.amazon.com/cli/latest/reference/devops-agent/).
- **D-release:** [Release management capabilities](https://docs.aws.amazon.com/devopsagent/latest/userguide/working-with-devops-agent-release-management-index.html).
- **D-assets:** [Assets, including test-profile schema](https://docs.aws.amazon.com/devopsagent/latest/userguide/about-aws-devops-agent-managing-assets.html).
- **D-price:** [DevOps pricing](https://aws.amazon.com/devops-agent/pricing/).
- **S-CLI:** [Security Agent CLI operations](https://docs.aws.amazon.com/cli/latest/reference/securityagent/).
- **S-overview:** [Security/Continuum capabilities](https://docs.aws.amazon.com/securityagent/latest/userguide/what-is.html).
- **S-price:** [Security pricing and billable capabilities](https://docs.aws.amazon.com/securityagent/latest/userguide/pricing.html).
- **S-pentest-price:** [Continuum penetration-testing pricing](https://aws.amazon.com/continuum/pricing/).
- **W-start:** [Well-Architected Agent prerequisites](https://docs.aws.amazon.com/wellarchitected/latest/userguide/agent-getting-started.html) and [API quickstart](https://docs.aws.amazon.com/wellarchitected/latest/userguide/agent-api-quickstart.html).
- **W-generation:** [Start recommendation generation CLI](https://docs.aws.amazon.com/cli/latest/reference/wellarchitected/start-agent-recommendation-generation.html).
- **W-quotas:** [Support entitlements and quotas](https://docs.aws.amazon.com/wellarchitected/latest/userguide/agent-quotas.html).
- **Support-price:** [AWS Support pricing](https://aws.amazon.com/premiumsupport/pricing/).
- **F-overview:** [FinOps capabilities](https://docs.aws.amazon.com/finops-agent/latest/userguide/what-is.html).
- **F-create:** [FinOps creation and console access](https://docs.aws.amazon.com/finops-agent/latest/userguide/creating-an-agent.html).
- **F-setup:** [FinOps IAM and dependent-service setup](https://docs.aws.amazon.com/finops-agent/latest/userguide/setting-up.html).
- **F-faq:** [FinOps availability, preview pricing and integrations](https://aws.amazon.com/finops-agent/faqs/).
- **T-cli:** [Transform custom command reference](https://docs.aws.amazon.com/transform/latest/userguide/custom-command-reference.html).
- **T-continuous:** [Transform continuous-modernization commands](https://docs.aws.amazon.com/transform/latest/userguide/ct-working-with.html).
- **T-price:** [Transform pricing by capability](https://aws.amazon.com/transform/pricing/).

[D-CLI]: https://docs.aws.amazon.com/cli/latest/reference/devops-agent/
[D-release]: https://docs.aws.amazon.com/devopsagent/latest/userguide/working-with-devops-agent-release-management-index.html
[D-assets]: https://docs.aws.amazon.com/devopsagent/latest/userguide/about-aws-devops-agent-managing-assets.html
[D-price]: https://aws.amazon.com/devops-agent/pricing/
[S-CLI]: https://docs.aws.amazon.com/cli/latest/reference/securityagent/
[S-overview]: https://docs.aws.amazon.com/securityagent/latest/userguide/what-is.html
[S-price]: https://docs.aws.amazon.com/securityagent/latest/userguide/pricing.html
[S-pentest-price]: https://aws.amazon.com/continuum/pricing/
[W-start]: https://docs.aws.amazon.com/wellarchitected/latest/userguide/agent-getting-started.html
[W-generation]: https://docs.aws.amazon.com/cli/latest/reference/wellarchitected/start-agent-recommendation-generation.html
[W-quotas]: https://docs.aws.amazon.com/wellarchitected/latest/userguide/agent-quotas.html
[Support-price]: https://aws.amazon.com/premiumsupport/pricing/
[F-overview]: https://docs.aws.amazon.com/finops-agent/latest/userguide/what-is.html
[F-create]: https://docs.aws.amazon.com/finops-agent/latest/userguide/creating-an-agent.html
[F-setup]: https://docs.aws.amazon.com/finops-agent/latest/userguide/setting-up.html
[F-faq]: https://aws.amazon.com/finops-agent/faqs/
[T-cli]: https://docs.aws.amazon.com/transform/latest/userguide/custom-command-reference.html
[T-continuous]: https://docs.aws.amazon.com/transform/latest/userguide/ct-working-with.html
[T-price]: https://aws.amazon.com/transform/pricing/
