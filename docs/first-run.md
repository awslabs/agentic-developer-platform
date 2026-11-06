# First run after installing ADP

Use this after the full deployment, including the cloud agent stack. Have the
installer's Frontend URL, your team's personal GitHub usernames, and a test
repository ready. CLI examples assume the CLI is installed and `adp login` has
selected the same deployment and organization as your browser; see
[CLI setup](adp-cli/README.md). Replace uppercase placeholders with your values.

## Organizations, teams, and people

| Concept | What it means in ADP |
| --- | --- |
| ADP organization / workspace | The organization whose membership, connections, and settings you are using. Select it using the dashboard's **Organization** selector. |
| GitHub organization | A GitHub repository owner. It can own the GitHub App or an installation; its name need not match the ADP organization. |
| Department and team | Departments group teams inside an ADP organization. A person can belong to several teams; their primary team determines budgets and Bedrock routing. |
| User and membership | A person's login and their access to a particular ADP organization are separate. Connecting a GitHub repository does not add its collaborators as ADP users. |
| Platform admin | Deployment-wide administration, including organization creation, platform user placement, and platform model defaults. Keep the bootstrap account for this work. |
| Org admin | Administration within an authorized ADP organization, including its structure and eligible access requests. It does not grant platform-admin powers or access to the platform-wide user picker. |
| Member / department admin | A member has ordinary scoped access, without organization administration. The additional `dept_admin` role has administration scoped to its department. |

GitHub ownership does not make someone an ADP platform admin. ADP membership and
roles are assigned or approved separately; neither ADP role grants GitHub ownership.

### 1. Sign in as the first administrator

Use the [README credential command](../README.md#run-coding-agents-in-the-cloud-1)
against the installation account, then open **Frontend URL → Sign in with Email**.
Enter the secret's `username` and `password`, or your custom administrator
credentials. MFA defaults to **OFF**; an operator can change that deployment setting.

**Success:** the dashboard opens with administrator controls. Keep this session
open until GitHub sign-in has been tested.

### 2. Create the organization and its teams

In **Organizations** (`/admin/organizations`), select an existing organization or
choose **+ Create organization → Name / Identifier → Create organization**.
Review the suggested identifier: it cannot be changed later. No GitHub connection
is needed. **Success:** the organization appears with its default department and team.

To add structure, select that organization and open **Structure → Add Department**,
enter **Name**, then choose **Add department**. Under **Teams in department**, select
the department, choose **Add Team**, enter **Team Name**, then **Create**.
**Success:** the new department and its team appear in Structure. The default team
is sufficient for a first run; additional teams are optional.

See the canonical [hierarchy CLI guide](adp-cli/hierarchy.md) for creation commands,
membership readback, revisions, and role restrictions. These steps correspond to
the current [Organizations UI](../modules/gateway/frontend/src/pages/admin/Organizations.tsx)
and [authorization rules](../modules/gateway/src/admin/access_control.py).

### 3. Add yourself and your teammates

As platform admin, open **Organizations → select organization → Members → + Add member**.
Choose the appropriate **Add by** option:

| Available path | Exact action | Success signal |
| --- | --- | --- |
| Existing ADP person | Choose **Existing ADP person**, search/select the person, select **Team** and **Role**, then **Add member**. If joining from another organization, explicitly confirm **Add to organization and team**. | The person appears in this organization's Members list with the intended team and role. This picker only assigns existing ADP users; it is not an invitation form. |
| Personal GitHub username | Choose **GitHub username**, enter the individual's username, choose **Team** and **Role** (**Member** or **Org admin**), then **Assign GitHub user**. Repeat for your own GitHub account and teammates. | Assignment completes and the member appears. After GitHub login is configured, they can sign in without an access request. No invitation email is sent; share the dashboard URL. |

The second path can create an ADP identity before first sign-in in the current
source. It verifies the personal GitHub account and refuses conflicting login
bindings or primary-team changes; it does not merge a native email login with a
GitHub login. If assignment reports that login synchronization failed, follow its
retry instruction with the same username, organization, and team before testing
login. See the [GitHub enrollment implementation](../modules/gateway/src/admin/identity/github_enrollment.py).

For existing members, **Members → Manage teams → Add to team** adds another
membership. **Success:** both memberships remain visible and the intended primary
team is marked. Adding a secondary team does not move the primary team's budget
or routing assignment.

**Available now versus product work:** the existing-person picker does require an
existing ADP user, but that restriction does not apply to all provisioning paths.
The current CLI also supports native-user creation (platform admin):

```bash
adp admin member add --org ORG_ID --new-user --email member@example.com \
  --name "New member" --team TEAM_ID --role member --json
```

Confirm the prompt. **Success:** the returned membership identifies the intended
organization, team, and new user; provisioning errors require recovery before
login. This CLI path suppresses invitations. The
[native Cognito onboarding API](admin/native-cognito-onboarding.md)
supports explicit invitation delivery and recovery. An invitation-email field in
the Members picker or automatic native/GitHub account merging would require product
work; neither is a hidden step in this flow. If your installed version lacks the
GitHub username option, use its supported provisioning/access-request path or
upgrade before following that option.

### Access requests and approval

For a user admitted to sign-in but not yet registered, the onboarding page
(`/onboarding/welcome`) asks **Why do you need access? → Request Access**.
**Success:** **Request Under Review** appears, or the dashboard opens if an
applicable policy approves immediately. The welcome form submits a reason; it
does not let the user choose an arbitrary organization. The service derives the
target. Organization-targeted requests also exist through the access API/CLI;
see [access and sessions](adp-cli/access-and-sessions.md).

An administrator opens **Access Requests** (`/admin/access-requests`), checks the
request's target organization, and chooses **Approve**, or **Deny → optional
reason → Deny**. Org admins can decide eligible requests to join their own existing
organization; new-organization requests require a platform admin. Approval does
not indiscriminately grant org-admin privileges: the server derives the allowed
role. **Success:** the decision toast appears and the request leaves the pending
list. The approved user selects **Check Status** on the pending page and reaches
the dashboard; verify the resulting organization, team, and role in Members.

This is not guaranteed open signup: sign-in eligibility may reject an unassigned
user before this page, and disabled onboarding shows **Onboarding Not Available**.
Use the platform-admin assignment path above in that case. The
[request and approval handler](../modules/gateway/src/admin/onboarding/handler.py)
defines the target and role checks.

## Connect GitHub in this order

1. **Create/register the App.** In the bootstrap session, open **Settings →
   Connections → Set up GitHub App**. Choose **Organization** and its GitHub name,
   or **Personal account (user-owned)**, then **Create on GitHub**. Complete GitHub's
   approval; an organization-owned App requires the appropriate GitHub owner.
   This App configures agent access and GitHub OAuth sign-in. **Success:** the App
   is registered without **GitHub sign-in not wired**; run **Re-validate config**.
   Reuse an already registered App, or follow the
   [existing-App import instructions](adp-cli/github.md#import-an-app-another-github-administrator-created).
2. **Test login before repository installation.** In a private/separate browser
   window, open the dashboard → **Sign in with GitHub** using the personal account
   assigned earlier. **Success:** the intended ADP organization is accessible with
   the assigned role. If rejected or asked for access, check the assignment from
   the bootstrap session.
3. **Select the destination workspace, then install.** In the GitHub-authenticated
   session, select the intended ADP **Organization** at the top of the dashboard.
   Open **Settings → Connections → Install on GitHub** (or **+ Add another
   connection**), choose the GitHub owner and **Only select repositories**, select
   the test repository, and complete installation/any owner approval.
   **Success:** Connections lists the installation and repository under the
   intended ADP organization. With the CLI, run `adp github status --repo OWNER/REPO`;
   its result must confirm coverage for that exact repository.

For an existing installation, use **Settings → Connections → Already installed
on GitHub?**. Enter the ID from **GitHub Settings → Applications → Configure**
(the number at the end of its URL), then **Connect to selected ADP organization**.
The same organization/repository readback is the success check.

One GitHub installation has one owning ADP organization, even if their names
differ. Starting installation directly on GitHub does not select or create an ADP
organization or grant membership; return to ADP to complete the connection.
An installation already owned by another ADP organization cannot be silently
reassigned. See [GitHub organization selection](adp-platform-deployment/github-organization-selection.md)
for the canonical binding and recovery rules, implemented in the
[Connections UI](../modules/gateway/frontend/src/pages/settings/components/GitHubTile.tsx).

## Choose an agent and trigger

A persona defines the agent's job and runtime; its model is a separate selection.
Use one trigger for your first task, from a GitHub account assigned to ADP.

| Job | Mention in an issue/PR comment | Issue label trigger |
| --- | --- | --- |
| Implement changes | `@agent-developer` | `developer` |
| Review changes | `@agent-reviewer` | `agent-reviewer` |
| Architecture/design | `@agent-architect` | `agent-architect` |
| Product analysis | `@agent-product` | No label mapping |
| Project coordination | `@agent-pm` | `pm` |
| Operations | `@agent-operations` | `agent-operations` |

Labels in this table dispatch on the issue's **labeled** event; they are exact
names, not a general `agent-` naming convention. Other personas include AI-DLC,
intent refinement, malware analysis, Superpower, and Superplane operator/researcher.
AI-DLC accepts `@agent-aidlc` or an issue **opened with** `aidlc-intent` already set.
Native Codex personas use `@agent-codex-developer`, `@agent-codex-reviewer`,
`@agent-codex-architect`, `@agent-codex-product`, `@agent-codex-pm`, and
`@agent-codex-intent-refinement`. The shorter `@agent-codex` is a Claude SDK
supervisor. Eligible PR events can also start the native Codex reviewer automatically.
Deployment configuration and authorization still govern admission.

See the [agent catalogue](agent-catalogue.md) for persona responsibilities and
runtime setup. The [persona registry](../modules/agent-factory/webhook-ingress/lambda/common/personas.py)
and [event parser](../modules/agent-factory/webhook-ingress/lambda/github/intent_parser.py)
define current trigger names. **Success for admission:** the intended persona's
run appears in **Agent Activity**; completion requires checking its result.

For a hosted planning conversation, use this alternative to a mention:

```bash
adp flow start "Add a greeting document" --repo OWNER/REPO --issue ISSUE_NUMBER
```

Answer its questions, inspect the proposed plan, and accept the reviewed revision
when ready. **Success:** an accepted flow ID is returned; follow it with
`adp flow watch FLOW_ID` and verify execution and artifacts. Plan acceptance alone
does not prove a worker ran. The deployment needs the hosted planning/execution
services enabled. See the [flow guide](../modules/gateway/cli/flow.md) and
[flow recovery](adp-cli/flow-recovery.md); leaving a watch does not cancel the flow.

## Choose and check models

In **Settings → Agent Models**, choose **My own agents**, find the persona, then
**Change model → select a compatible model → Save model**. **Success:** **Model for
new runs** shows the choice and its source. **Use default** removes your preference.
Changes apply to new runs; running work retains its selected configuration.

The equivalent CLI inspection and change sequence is:

```bash
adp models catalog --persona developer --json
adp models mappings list --json
adp models explain --persona developer --json
adp models mappings set --persona developer --model sonnet5 --dry-run --json
adp models mappings set --persona developer --model sonnet5
adp models explain --persona developer --json
```

Choose an available model from the served catalogue; `sonnet5` is an example alias
for `global.anthropic.claude-sonnet-5`. The dry run validates without saving or
invoking a model. Confirm the subsequent save prompt. **Success:** `explain` reports
the intended `effective_model_id`, `source`, and availability. Review any
unavailable/candidate reason; a selectable catalogue entry is not evidence that
this AWS account has successfully invoked it. To inherit again, run
`adp models mappings reset --persona developer`, confirm, and re-run `explain`.

Mappings belong to the initiating human in the selected organization, including
delegated work. An admin changing **My own agents** changes their own mapping,
not everyone else's. For a managed service account, select that account in the UI,
or use `adp models service-principals list --json` and add
`--service-principal PRINCIPAL_ID` to catalog/mapping/explain commands. The server
checks authority to manage that principal. **Success:** readback for that same
principal shows its intended mapping.

When mapping is enabled, a supported explicit run override takes precedence over
the principal's saved persona preference, then the platform persona default,
then the SDK-class default. Runtime compatibility, destination access, and policy
can still reject the choice. `explain` shows current settings, including
`default_model_id` and `default_scope`; it is not a historical run receipt.

A platform admin changes a persona-wide default through **Settings → Agent Models
→ Platform persona defaults → persona's model selector → Reason for change →
Save platform default**. **Use SDK default** restores inheritance. **Success:**
**Reload defaults** shows the saved value; a user without a personal override sees
it in `explain`. Personal preferences still take precedence. Promotion requires
the server's SDK/destination evidence checks. For SDK-class defaults, inspect with:

```bash
adp admin models default show --compatibility-class claude-agent-sdk --json
```

Follow [model policy](adp-cli/model-policy.md) for the versioned CLI default-change
procedure. A class default and a persona default have different scopes.

**Shipped fallbacks are not a deployment readback.** The Claude worker's fallback
is `global.anthropic.claude-sonnet-5`; native Codex developer and architect fallbacks
are `openai.gpt-6-sol` and `openai.gpt-6-astra`, respectively, unless their runtime
environment overrides them. Configured mappings/defaults can replace these.
An unset mapping may report `runtime-default`, while protected policy can refuse
missing or unproven defaults. Check the deployment's `explain` output and actual
run usage instead of assuming a fallback executed.

See [persona-model mapping](adp-platform-deployment/persona-model-mapping.md) for
deployment wiring. Current precedence is defined by
[dispatch selection](../modules/gateway/src/agentauth/launch_configuration.py) and
[default resolution](../modules/gateway/src/admin/persona_models/service.py);
the [Agent Models UI](../modules/gateway/frontend/src/pages/settings/AgentModels.tsx)
and [platform-default controls](../modules/gateway/frontend/src/pages/settings/PlatformPersonaDefaults.tsx)
define the settings workflow.

### Bedrock readiness

The selected model must be available in the destination AWS account and region,
with the required model entitlement/Marketplace agreement, Anthropic first-use
registration where applicable, inference profile, and runtime IAM permissions.
Full deployment includes model-access preparation; choosing a model in ADP does
not grant AWS access. Check the shipped defaults from the repository root:

```bash
AWS_PROFILE=YOUR_PROFILE AWS_REGION=us-east-1 \
  bash platform/scripts/enable-bedrock-models.sh --check
```

**Success:** required models report agreement, authorization, entitlement, region,
and profile readiness, and the command exits successfully. This check performs
no inference or mutations; it does not prove that the worker's routed credentials
can complete a task. See [Bedrock first run](adp-platform-deployment/bedrock-first-run.md)
for registration, restrictions, and bounded paid `--verify` probes. Recheck the
actual destination when using organization/team routing or additional models.

## Run and verify a small task

In the connected repository, choose **Issues → New issue**, title it
**ADP first run: add a greeting document**, and use:

```text
Create docs/adp-first-run.md containing one line: Hello from ADP.
Open a pull request linked to this issue. Keep the change to that file.
Do not merge or deploy it.
Validation: the PR diff contains the file and the exact greeting.
```

Post `@agent-developer please implement this issue.` once, or choose one of the
alternative triggers above. Follow the issue comments and **ADP → Agent Activity**.
**Success:** the resulting PR links the issue and **Files changed** contains only
the requested greeting document. An “Agent started” comment is not completion.
If nothing arrives, inspect the run status, exact repository connection, and
**Settings → Connections → Re-validate config**. Operators can continue with the
[deployment reference](adp-platform-deployment/deployment-reference.md).

### Check which model actually ran

Find the run in **Agent Activity**, or use the CLI to obtain its invocation ID:

```bash
adp agent list --json
adp agent status --run RUN_ID --json
adp usage requests --run RUN_ID --start START --end END --json
```

Use a timezone-aware ISO8601 window covering the run (for example,
`2026-10-08T00:00:00Z` to `2026-10-09T00:00:00Z`; replace with your run's dates,
at most 90 days). The run filter is checked against the owner and organization.
**Success:** run-linked inference rows show `model`, `status_code`, and, where
recorded, `requested_model_id`, `resolved_model_id`, `resolution_source`, and
`model_decision_id`. Inspect successful requests; one task may have multiple
calls/models. A failed inference request does not prove successful execution,
and a successful inference request does not prove the task produced its PR.

Follow `detail.next_cursor` with `--cursor CURSOR` and the same run/time filters
if results are incomplete. Missing attribution is unknown;
do not substitute today's mapping for historical evidence. See
[agent commands](adp-cli/agent.md) and [usage queries](adp-cli/usage.md) for filters,
pagination, and evidence limits. The [usage response schema](../modules/gateway/src/usage/cli_reads.py)
defines the recorded model fields.
