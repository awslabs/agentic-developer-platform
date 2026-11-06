# Verdict: amendment

The owner approved temporary EC2 job instances on 2026-11-01 for
[story #9276](https://github.com/aws-e/adp/issues/9276). Use GitLab's standard
Docker Autoscaler executor with AWS Fleeting. Managers remain on EKS; jobs use
private disposable EC2 instances in the **same VPC as EKS**, with dedicated
security groups and no public IPs. This is the current implementation contract
for S2 and the corresponding S3/evaluation changes. It supersedes the native
Kubernetes executor, Auto Mode job NodePool and Custom executor alternatives in
the earlier design. S1 registration/CA and its accepted evidence are retained.

**Readiness:** implementation can proceed. This decision is not evidence of a
working deployment, release compatibility, enforcement or live acceptance.
The module owner implements and reviews S2; the delivery owner integrates S3
and performs authorized deployment and qualification. Existing saved-plan,
IAM/PassRole, teardown and live-readiness controls remain in force.

## Evidence for the executor choice

- AWS [network-policy considerations](https://docs.aws.amazon.com/eks/latest/userguide/cni-network-policy.html)
  warn about standalone pods; Runner 19.5.2 creates that lifecycle.
- AWS [Security Groups for Pods](https://docs.aws.amazon.com/eks/latest/userguide/security-groups-for-pods.html)
  excludes Auto Mode.
- GitLab [Custom executor at 19.5.2](https://gitlab.com/gitlab-org/gitlab-runner/-/blob/v19.5.2/docs/executors/custom.md)
  is in maintenance mode and recommends active executors for new projects.
- GitLab [Docker Autoscaler at 19.5.2](https://gitlab.com/gitlab-org/gitlab-runner/-/blob/v19.5.2/docs/executors/docker_autoscaler.md)
  documents AWS Fleeting, dedicated autoscaling groups and disposable instances.

Vendor documentation supports this executor choice; it does not qualify our
network, image, metadata, privilege or cleanup implementation.

## S2 implementation contract

1. Preserve the separate runners backend and read-only platform/S1 discovery.
   Preserve numeric tenant-group registration, private HTTPS rooted URL, runtime
   tmpfs token/CA loader, strict TLS and group cache separation. Instance state
   has no dependency on runners. No token/CA values in Terraform/Helm state,
   image layers, persistent Kubernetes Secrets, logs or public evidence.
2. Deploy one pinned manager on EKS per approved group, with its own namespace,
   service account and IRSA role. Disable unused Kubernetes job permissions.
   Use exactly one dedicated ASG controlled only by that manager. Managers run
   on trusted platform capacity; EC2 job instances do not join EKS.
3. Set `capacity_per_instance = 1`, `max_use_count = 1`, `idle_count = 0` and
   `max_instances <= 2` per group. ASG min/initial desired are zero. Preserve the
   aggregate ceiling of 16 vCPU/64 GiB across groups, enforce it using allowed
   instance-type capacities, and preserve bounded per-job resources/timeouts.
   A manager restart must not reuse a previously assigned instance. Terminate
   completed/failed/cancelled and orphaned job instances within 15 minutes;
   retain a healthy manager. An independent scoped reconciliation path must
   handle manager loss without deleting unrelated instances.
4. Validate that every selected subnet and security group belongs to the EKS
   VPC; use private subnets and disable public IP allocation. No new VPC or
   peering. Manager-to-host connection is limited to the pinned Fleeting
   connector and dedicated group SG; no public SSH or job-visible Docker socket.
5. Implement a trusted, pinned Docker host bootstrap. Apply persistent default
   deny and metadata denial before Docker/Fleeting accepts any job. Cover host
   and forwarded container traffic, IPv4/IPv6 and Docker rule ordering. SGs
   alone do not isolate link-local metadata or individual containers; IMDSv2
   alone is insufficient. The module owner must prove startup ordering and
   behavior during bootstrap failure and daemon restart, not assume user data
   finished before SSH became reachable. Failure leaves the runner paused.
6. Allow only qualified GitLab HTTPS, required registry/helper pulls, per-group
   cache and controlled DNS. Use endpoint policies and host/container controls
   as necessary. Reject whole-VPC and arbitrary-internet egress, alternate
   DNS/DoH, direct-IP bypass, platform services and other groups. Jobs receive
   no AWS/Kubernetes identity; no manager token, IRSA material, connector keys or
   host credential files are mounted/exposed to containers. Any host role for
   registry/bootstrap is minimal and inaccessible to jobs.
7. Enforce `privileged = false`, non-root jobs/helpers/services, no host
   networking/PID/IPC, no host paths/devices, no added capabilities or Docker
   socket, no arbitrary images/services/config overrides or debug tracing.
   Lock allowed images by digest and apply Docker security/resource controls
   to every job container. Pre-review tests must exercise the actual pinned
   executor configuration, including plausible bypass requests. Invalid GitLab
   YAML is not evidence of privilege denial.
8. Manager IAM is limited to its ASG/owned-instance lifecycle, connector,
   exact group token/CA and own cache prefix. Resource-unscopable Describe
   actions require documented regional/tag restrictions where supported.
   Prepare exact action/resource and PassRole role/service pairs from the
   concrete Terraform plan; do not grant wildcard PassRole or copy platform
   roles. Job code cannot call STS, Secrets Manager or cluster APIs.
9. Complete the release lock: reviewed chart archive/hash, actual server/Runner
   compatibility, manager/helper/bootstrap/probe digests, Fleeting plugin
   version/digest, Docker version and AMI owner/ID/build provenance for each
   target architecture/region. Never invent digests or resolve `latest` at
   apply. The existing audited chart archive alone is not a complete lock.

Proposed S2 write scope under `modules/source-control/gitlab/runners/`:
`iam.tf`, `cache.tf`, manager `namespaces.tf`/`rbac.tf`/policy as needed,
`network.tf`, `autoscaling.tf`, `templates/job-user-data.sh.tftpl`, Helm values,
release lock and bootstrap. These replace job namespace/admission/NodePool
resources. Retain the existing metadata/group/loader roots and fixtures.
Add `tests/test_rendered_runner.py` and `tests/test_job_host.py` for the actual
rendered chart, host startup/deny ordering, capacity, IAM and lifecycle behavior;
retain `tests/security_contract.tftest.hcl` and `tests/test_bootstrap.py`.
These tests/paths are deliverables, not claims that they already exist or pass.

## S3 integration and verification contract

Retain manual protected apply/destroy, credential-free PR checks, reviewed-main
build, scoped build identity, shared concurrency, saved-plan guards, registration,
rotation and runbook. Extend trusted build/pinning for the EC2 host image and
Fleeting plugin without adding a dependency on GitLab CI itself. Build permissions
and infrastructure remain separately reviewed; no source-control instance change.

The same stable acceptance/scenario IDs now use these observation sources:

| Stable IDs | Required behavior and independent evidence |
|---|---|
| #9276 AC-01; #9285 AC-01; EC-02 | GitLab A job reports its expected group runner; identical-tag B stays pending for five minutes while A works, then cancel B. Correlate GitLab job ID with manager logs and owned EC2 instance ID; job output is not its own sole oracle. |
| #9276 AC-02; #9285 AC-02; EC-03 | Actual Docker job cannot reach gateway, Dev Box, RDS/Redis, other tenants, metadata IPv4/IPv6, cluster API/STS/SM, arbitrary internet/proxy or DNS bypass. Confirm listening negative targets independently and successful GitLab/cache controls; the approved IPv4-only metadata amendment below is the sole exception to the listening IPv6 reference target. Verify container inspect/configuration and attempted privilege/host/socket/identity/image overrides through the real executor. |
| #9276 AC-03; #9285 AC-01; EC-02 | Within 15 minutes of legal/failed/cancelled jobs, every owned ASG has zero desired/current/pending/terminating job instances and EC2 inventory has no owned nonterminated instance; manager Deployment stays READY 1/1. Include manager-loss cleanup. Empty Kubernetes job namespaces are not EC2 lifecycle evidence. |
| #9276 AC-04; EC-04 | Post-S1 instance plans at fixed source/private inputs are no-op with runners absent, present and after authorized teardown; inspect state/resource ownership and revoke runner tokens before cleanup. |
| #9276 AC-05; EC-04 | Real loader/manager/helper wrong or missing CA fails, correct-root/name succeeds, cross-origin redirect and insecure settings fail; waited apply fails readiness and never activates the runner. |
| #9285 AC-03; EC-01/04 | Synthetic canaries through success/failure paths absent from full logs, state and artifacts; inspect live evidence privately. |
| Q1; #9276 AC-02; EC-03 | Three fresh EC2 starts and one Docker/enforcement-service restart: denial holds from the actual job's first instruction; trusted host evidence proves controls installed before accepting work. A failed bootstrap is never schedulable. No restart may remove default deny. |
| #9276 SCN-01/02; #9285 SCN-01 | Preserve offline versus live phases, all attempts, pinned revisions/digests, 10-minute pipeline/5-minute B/15-minute cleanup bounds and 3-second connection timeouts. Missing/blocked/skipped required cases mean incomplete acceptance. |

S3 delivers `tests/acceptance/.gitlab-ci.yml`, `probe.py`, `verify.py`,
`fixtures.schema.json`, `executor-negative-cases.json` and
`tests/test_verify.py` under the GitLab module. The negative fixture replaces
Kubernetes `privileged-pod.yaml` / `override-pods.yaml`; inspect actual Docker
configuration and behavior, not Kubernetes admission that jobs do not use.
The trusted verifier uses GitLab, EC2/ASG and manager Kubernetes APIs; check
wrong group, one orphan remaining, wrong VPC, missing early-denial evidence and
missing negative cases as counterexamples. Public reports contain sanitized
results and source revisions; exact environment evidence stays private.

## Delivery flow and gates

Preserve Wave 1, the existing story/PR binding, attempts, budget accounting,
AC/SCN identifiers, dependencies and human evaluations. Update #9276, #9285,
#9289, #9290 and #9291 bodies to this contract. PR #9436 remains incomplete until
S2 implementation and repository checks pass; its offline checkpoint is reusable.
No fresh development dispatch or acceptance is implied by this amendment.
The active assignment must not be invalidated merely to relabel the graph.
The owner-approved issue/design amendment governs implementation; retained plan
labels describe the original EKS-only design until a supported metadata amendment
can preserve the active assignment. No direct database edits or gate bypass.

Agent-run and flow spending limits remain unchanged. They do not cover or approve
EC2 infrastructure spending. Live target/fixtures, exact IAM handoff, reviewed
saved plans and full qualification remain outstanding before activation.


## IPv4-only metadata qualification amendment

For an existing IPv4-only VPC, IPv6 metadata qualification may use the explicit
`disabled_by_provider` mode. Terraform disables `http_protocol_ipv6`; fresh EC2
instance and exact ASG launch-template-version observations must both confirm
that setting, no job IPv6 interfaces and no job IAM profile. Each observation
is tied to the same run, source, instance and AWS request identity. The trusted
host must verify the pinned IPv6-deny policy before admission and after the
required Docker/firewall restart. The actual job still executes its IPv6
metadata connection attempt and retains the real outcome and errno. The
verifier rejects stale/missing/mismatched observations and reachable transports.

This is a distinct provider-disabled result, never a skip, N/A or an invented
firewall timeout. Every other target retains its listening control and packet
denial requirements. Cold starts, restart, bootstrap failure, identity, cleanup,
instance independence, deployment and teardown gates remain required. See the
module runner runbook for the concrete evidence fields and collection order.
