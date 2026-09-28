**AI-DLC feasibility assessment — security run of 21 September 2026**

Yes: ADP’s AI-DLC graph can represent the 21 work packages under [today’s epic #5599](https://github.com/aws-e/adp/issues/5599), preserve their dependencies, and dispatch ready work. I validated that mapping against the deployed engine. Driving all 21 packages through review, merge, deployment, and verified completion without intervention needs additional configuration and some completion-path work.

This assessment registered no flow, accepted no execution policy, launched no developer agents, and changed no scan triggers. All preview calls used the API that writes no state. The attached topology is an analysis artifact, not a launch-ready execution plan.

**What was verified**

The inspected source and live gateway are at `10cc916c634bc4feffd4a0455ae557275f709d6a`. The gateway has three ready replicas with orchestration enabled. The active `adp-dev-orchestration-tick` Lambda uses image digest `sha256:e8de4b82874be5f1a3f68bfd587a4c50f9a0d2c678e8039d0c0eb2558a3e6102`, which ECR associates with that same commit. Its EventBridge schedule is enabled every five minutes. This is the engine’s polling schedule; it does not enable scheduled security scans.

The authenticated user has platform-admin access in tenant `aws-e`. Repository access to `aws-e/adp` is verified, and the tenant has one GitHub installation (`124731131`), satisfying the dispatcher’s unambiguous-routing requirement. Existing flow state and successful tick logs show the service is operating; they do not prove this security workload will complete end to end.

The deployed preview rejected the raw 21-story plan because each wave containing stories requires exactly one evaluation node. Adding a final evaluation produced `would_register: true`, no violations, and `wrote_nothing: true`. The authored graph has 22 nodes and 23 edges. The server adds the initial acceptance gate and its 15 outgoing links, producing 23 nodes and 38 edges. All 22 original issue dependencies are preserved exactly.

Evidence: [topology proposal](ai-dlc-topology-preview.json), [successful server preview](ai-dlc-with_evaluation-server-preview.json), [initial rejection](ai-dlc-raw-server-preview.json), and [deployment coverage probe](ai-dlc-deployment-coverage.json).

**How to submit the existing issues**

Use a file-based `LoopProposal`, with one story node per existing GitHub issue, numeric `issue_ref` values, `org_id: aws-e`, an intent reference to `5599`, and explicit `from_address` → `to_address` edges. Keep the published [work-package plan](https://github.com/aws-e/adp/blob/42ade12e0f09570179c86a75a1c224cabab77cb4/docs/security/runs/2026-09-21/work-packages.md) as the pinned scope/evidence reference. There is no need to recreate the 21 issues.

The file import does not automatically read GitHub child-issue or blocker relationships. Those relationships must be translated into graph edges, as in the attached proposal. Starting a generic planning flow from just `--issue 5599` is not equivalent to importing this reviewed plan.

The supported sequence is preview → register an inert draft → inspect the effective graph and proposed policy → accept that exact plan hash → watch the flow. A submitted execution policy remains proposed until acceptance. A file can be submitted with `adp flow create --file <ready-plan.json>` after the CLI is current. This machine’s installed CLI predates `adp flow`; the helper in the inspected checkout supports it. The installed CLI was not changed.

The preview here intentionally contains no execution policy and no GitHub issue for its evaluation node. The response consequently says `execution_is_unbounded: true`; its default evaluation is human. These are explicit reasons not to launch the attached file unchanged. Its successful preview establishes graph validity, not runtime readiness.

**Dependency order**

The engine schedules by predecessor node state, not issue number or list order. For a policy-enabled story, the merge controller marks its graph node passed at verified PR merge while its execution continues through deployment and evaluation. These are therefore coding eligibility groups, not mandatory global batches or proof of deployed acceptance: a dependent can start as soon as its own predecessors pass.

| Eligibility | Work packages |
|---|---|
| Initially ready after acceptance: 15 | S01–S10 (#5600–#5609), S14 (#5613), S16–S17 (#5615–#5616), S19–S20 (#5618–#5619) |
| After S10 verified caller identity | S11 tenant identity (#5610), S13 admin/Cognito (#5612), S15 ingestion/ACLs (#5614) |
| After S11 | S12 vault/worker authority (#5611) |
| After S13 | S18 budget/settlement (#5617) |
| After all remediation branches | S21 integration and final scans (#5620), followed by evaluation |

S21 has 17 direct predecessors; the remaining three upstream packages are covered transitively. Scope ownership remains important: engine work claims prevent duplicate work on the same issue, but do not prevent different issues from editing the same file. S21 retains ownership of shared release pins and global scanner dispositions. Start with roughly three concurrent work streams as a recommendation, and set actual action concurrency, spend, time, and attempt limits in the accepted policy.

**What prevents full unattended delivery today**

| Area | Concrete finding | Required work |
|---|---|---|
| Execution authority | No bounded policy has been prepared or accepted for this epic. The engine supports separate develop, review, repair, merge, deploy, and evaluate permissions. | Specify repository and environment scope, permitted actions, expiry, spend, wall-clock, attempts, concurrency, and any retained human gates. Omitting the policy selects legacy behavior; it is not a completion solution. |
| Component coverage | The path classifier does not map Superplane API/controller/SkyPilot, ingestion, cyber, CI IAM, or scanner paths in the probe. Agent-worker and documentation classify but have no deployment manifest entry. Other mixed-component packages need full changed-path coverage too. | Add reviewed component mappings, deployment/documentation entries, and verification support for every actual change surface. |
| Deployment targets and revisions | Both shipped gateway manifest entries are `unresolved`. Runtime entries also require `artifact_revision` to equal the actual merged commit. | Resolve registered connections and physical targets, approve workflow inputs, and provide a supported way to authorize each release revision. Merely enabling the existing manifest once does not authorize arbitrary future merges. Fully unattended releases require resolving this approval model. |
| Evaluation | Machine evaluation requires a pinned harness, target, registered connection, fixtures, and mandatory criteria. The supported contract currently targets an AWS EKS namespace. | Define real security acceptance suites and evidence receipts. A prose instruction to rerun scanners does not satisfy the machine contract; non-EKS/IAM and image-only evidence may need contract/adapter extensions or explicit human evaluation. |
| Evidence-only outcomes | Some findings may already be fixed, false positives, or triage-only. Current story completion depends on delivery evidence, not simply an agent comment or a closed issue. | Define a legitimate evidence/disposition PR and documentation completion path, or implement an explicit evidence-only outcome. Do not require pointless source changes to manufacture completion. |

For policy-enabled stories, successful merge advances to `DEPLOYMENT_PENDING`; the legacy merged-PR observer deliberately cannot bypass deployment/evaluation. Currently registered runtime verification adapters cover gateway health and migration-head checks. No runtime rollback adapter is registered. Failures, expired limits, or human gates can therefore stop execution and require intervention; “drive to completion” means bounded progression with observable stops, not guaranteed endless repair.

**Adapt the graph to delivery semantics before launch**

The existing issue DAG is appropriate for coordinating implementation. It needs an additional pass for deployment and acceptance:

1. **Separate image preparation from shared release acceptance.** S21 owns final Superplane image pins while waiting on upstream image fixes. Because story graph nodes pass at verified merge, the current coding DAG does not establish a circular wait on those deployments. However, every upstream policy execution still expects its own deployment continuation. Define isolated image verification or a supported build-only delivery path, then let S21 own the shared release. Do not add upstream evaluation barriers that require the very shared pin changes S21 has yet to make; that would introduce an operational circular wait.
2. **Bind evaluations to all intended deployment evidence.** The current evaluation controller reads direct story predecessors, not all transitive ancestors. The proof’s evaluation directly follows only S21, so it does not establish independent acceptance of every earlier deployment. Machine evaluation also requires compatible targets across its predecessor receipts. Use waves/evaluations grouped by compatible deployment target, or extend the evidence model for cross-target acceptance; preserve the original issue dependencies and add explicit evaluation barriers where necessary. Each wave with stories must still have exactly one eval node.
3. **Make dependency completion explicit.** A story becomes graph-passed after verified merge while its execution can still await deployment and a managed evaluation. An edge from that story alone does not wait for deployed acceptance. Where downstream security work requires acceptance tests to have passed, add the corresponding eval dependency. Do not insert such barriers in ways that create a cycle with S21.
4. **Reconcile overlapping dispatch paths and late findings.** The nightly workflow also contains a root-dispatch delivery path. Reconcile its actual EventBridge wiring before launch so two systems do not independently remediate the same finding. The default bus lookup alone did not establish its state across all buses. Treat later AWS Security Agent findings as reviewed amendments to the accepted scope rather than silent new work.

**Recommended route**

First, complete readiness work covering bounded policy submission, release-revision authorization, missing component/target support, evaluation topology, and evidence-only completion. Then pilot one contained package—S07 frontend dependency remediation is a candidate once its gateway deployment and evaluation path are resolved—and verify actual develop → review/repair → merge → deploy → evaluate behavior. Check both engine state and GitHub PR/issue linkage during that pilot; issue closure alone is not acceptance evidence.

After the pilot, submit the remaining reviewed DAG with limited concurrency and appropriate evaluation barriers. S21 remains the shared integration/final-scan owner. Retain the user’s one-off scanning requirement: final validation should use explicit dispatches and should not enable scans on every PR. Gateway identity, credential, runner IAM, and budget fixes affect the engine’s own ability to operate, so verify those transitions before expanding concurrency.

**Source references at the inspected revision**

- [File import and revision-bound acceptance](https://github.com/aws-e/adp/blob/10cc916c634bc4feffd4a0455ae557275f709d6a/modules/gateway/cli/adp-flow.py#L1213); [non-writing preview route](https://github.com/aws-e/adp/blob/10cc916c634bc4feffd4a0455ae557275f709d6a/modules/gateway/src/orchestration/draft_routes.py#L762).
- [Proposal schema and validation](https://github.com/aws-e/adp/blob/10cc916c634bc4feffd4a0455ae557275f709d6a/modules/gateway/src/orchestration/proposal.py#L250); [execution policy limits](https://github.com/aws-e/adp/blob/10cc916c634bc4feffd4a0455ae557275f709d6a/modules/gateway/src/orchestration/execution_policy.py#L365).
- [Component mapping and deployment selection](https://github.com/aws-e/adp/blob/10cc916c634bc4feffd4a0455ae557275f709d6a/modules/gateway/src/orchestration/deployment_workflows.py#L89); [shipped target manifest](https://github.com/aws-e/adp/blob/10cc916c634bc4feffd4a0455ae557275f709d6a/modules/gateway/src/orchestration/manifests/orchestration-deployments.yaml#L99).
- [Merge-to-deployment transition](https://github.com/aws-e/adp/blob/10cc916c634bc4feffd4a0455ae557275f709d6a/modules/gateway/src/orchestration/merge_controller.py#L442); [policy-enabled completion handling](https://github.com/aws-e/adp/blob/10cc916c634bc4feffd4a0455ae557275f709d6a/modules/gateway/src/orchestration/results.py#L395).
- [Graph node passes at verified merge](https://github.com/aws-e/adp/blob/10cc916c634bc4feffd4a0455ae557275f709d6a/modules/gateway/src/orchestration/merge_controller.py#L528); [scheduler predecessor-state check](https://github.com/aws-e/adp/blob/10cc916c634bc4feffd4a0455ae557275f709d6a/modules/gateway/src/orchestration/tick.py#L193).
- [Evaluation predecessor receipts](https://github.com/aws-e/adp/blob/10cc916c634bc4feffd4a0455ae557275f709d6a/modules/gateway/src/orchestration/evaluation_plan.py#L52); [evaluation lifecycle and target compatibility](https://github.com/aws-e/adp/blob/10cc916c634bc4feffd4a0455ae557275f709d6a/modules/gateway/src/orchestration/evaluation_controller.py#L56); [machine evaluation contract](https://github.com/aws-e/adp/blob/10cc916c634bc4feffd4a0455ae557275f709d6a/contracts/orchestration-evaluation/v1/models.py#L74).
