# Qualification repository setup

Prepared 2026-10-21 for [ADP epic #8394](https://github.com/aws-e/adp/issues/8394).
The user requested repository provisioning through the current administrator
session. These are private, reusable test repositories. No delivery flow or
worker was started, and no dev deployment was performed by this setup.

| Role | Repository | Sample issue |
|---|---|---|
| Planning C | [adp-qualification-planning](https://github.com/aws-e/adp-qualification-planning) | [Epic: show service version through API and CLI](https://github.com/aws-e/adp-qualification-planning/issues/1) |
| API A | [adp-qualification-api](https://github.com/aws-e/adp-qualification-api) | [Story: add version endpoint](https://github.com/aws-e/adp-qualification-api/issues/1) |
| Client B | [adp-qualification-client](https://github.com/aws-e/adp-qualification-client) | [Story: display service version](https://github.com/aws-e/adp-qualification-client/issues/1) |

GitHub readback verifies both native parent-child links and the native dependency
from the client story to the API story. Both child issue numbers are `1`, giving
a real same-number/different-repository fixture. The issues are open and
unassigned. All 14 ADP implementation stories remain in `aws-e/adp`.

## Working baseline

- API: Python standard-library HTTP service, `/health`, JSON errors, two HTTP
  tests and a non-root container definition.
- Client: explicit service URL, `health` command, bounded requests and three
  executable CLI tests covering success and failure responses.
- Each code repository has GitHub Actions CI. The planning repo documents the
  integration command, expected result, ownership and retention.
- The `/version` endpoint and CLI `version` command are intentionally absent;
  those are the sample stories' future implementation work.

| Evidence | Result |
|---|---|
| [API baseline CI](https://github.com/aws-e/adp-qualification-api/actions/runs/39580924749) | Passed tests and container build at `e6a861c6474871fa971bb7a9acd74ca1cb95be38` |
| [Client baseline CI](https://github.com/aws-e/adp-qualification-client/actions/runs/39580934408) | Passed tests at `7915d1bb9aa5ddd63ff5936272700cb962ddfc5c` |
| Actual API plus actual CLI, local HTTP integration | Passed: exit 0, expected health JSON, independently confirmed through API readback |
| Native GitHub hierarchy and dependency | Passed provider readback |
| ADP installation access | Verified for all three through the existing tenant connection; earlier HTTP 403 resolved after the installation selection changed |
| Dev deployment and deployed integration | Not performed |

The local test is not deployed evidence. A later dev check must record the actual
deployment receipt and both tested revisions before accepting a live stage.

## ADP connection

ADP already has the tenant's GitHub installation record, credentials and webhook
routing. It reads the installation's repository list from GitHub; adding these
repositories does not require another manual per-repository database entry.
The earlier repository-selection API request returned HTTP 403. A later live
read observed the selection expanded, and ADP now verifies all three repositories.
The permission change was observed externally, not performed by this session.

The supported `adp --tenant aws-e github connect --repo OWNER/NAME --org aws-e`
path reuses this connection. `github status` independently reports live verified
access and configured agent integration. These checks do not run an agent or
establish Product-bound flow execution, which remains an implementation deliverable.
Knowledge indexing, if wanted, is a separate source registration; it is not
required merely to grant GitHub repository access.

## Agent smoke checks

On 2026-10-21 the user separately requested one smoke issue per repository and
an explicit developer-agent mention. All three agents started, acknowledged the
correct repository and published results; all three workers then exited.

| Smoke issue | Observed result | ADP worker result |
|---|---|---|
| [Planning #2](https://github.com/aws-e/adp-qualification-planning/issues/2) | Planning role and API/client links verified | Complete |
| [API #2](https://github.com/aws-e/adp-qualification-api/issues/2) | Two baseline tests passed, exit 0 | Complete |
| [Client #2](https://github.com/aws-e/adp-qualification-client/issues/2) | Three baseline tests passed, exit 0 | Failed because the developer completion contract required a PR |

This proves agent connectivity and execution, not a complete delivery flow. The
smoke request prohibited code changes and PRs. The developer completion path still
produced task-board-only PRs in planning/API; both were inspected and closed without
merging. Their branches remain for audit. The client respected the no-PR instruction
but its run was marked failed. This is a completion-contract mismatch, not an access
or baseline-test failure. All three default branch SHAs remain at their seeded
baselines. The sample version-feature issues are still unstarted.

## Remaining setup

1. Select and bind the sample service's dev target and deployment path, deploy
   the baseline, then run the client against that explicit dev URL. Repository
   creation does not supply deployment authority or credentials.
2. Use the already-created test Product on the authorized target. Product IDs,
   provider repository IDs, installation details and setup receipts remain in
   the operator's private inventory.

The larger delivery flow is still a draft under discussion. This setup does not
accept a flow policy or authorize the sample stories to execute. Early stages
can use these fixtures after access is verified; MR-13 later extends the existing
harness for the complete fault/recovery matrix. MR-14 preserves the operator's
manual live-workflow launch and final evidence review.

Retain the three repositories, baseline sample issues and reusable Product across
runs. Later qualification runs record them as pre-existing inputs and create
separate run-owned fixtures where needed. Cleanup must not delete these retained
resources or treat already completed sample stories as fresh work.
