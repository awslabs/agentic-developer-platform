---
name: skypilot
description: >-
  Use for issue-driven GPU capacity requests: express workload requirements,
  let SkyPilot select and provision machines, and prepare those machines for
  Superplane EKS workloads through the hybrid networking and node-join path.
metadata:
  domain: superplane
  upstream: aws-innovate/AISuperPlane/.claude/skills/skypilot
  authentication: existing ADP run and workspace connection
---

# SkyPilot for Superplane

The user describes a workload in a GitHub issue and tags an ADP agent. Turn that
intent into GPU requirements, use SkyPilot for machine selection and provisioning,
connect the machines to the workspace EKS cluster, then submit the workload and
report its result to the issue. ADP provides the existing run, identity, credential
delivery and authorized limits. A separate UI workflow is not required.

## Start with the issue

Use the issue and existing workspace context to determine the model or program,
acceptable GPUs, node count, target EKS cluster, runtime, cost ceiling and whether
the result should remain running. Ask only for material missing inputs. Honor
authorization already recorded for this run; do not ask for the same permission
at every step. Exceeding its target, duration or spending envelope needs a new
decision. An issue tag alone does not grant cloud credentials or unlimited spend.

For workspace discovery use the maintained `adp superplane` commands described in
the sibling `superplane` skill. Do not use the old standalone `superplane` CLI or
bootstrap a new SkyPilot server on the worker. Use the installed runtime and
existing scoped connection. Missing tools or credentials are installation gaps;
never put keys, activation codes or WireGuard private keys in issue text or task
YAML. Source-only work does not authorize live commands.

## Let SkyPilot select machines

Express accelerators and resource constraints. Leave the instance type, cloud and
region unspecified unless the user or workspace policy restricts them. Do not
rank a manually scraped GPU catalogue or write a replacement allocator.

SkyPilot's `any_of` expresses acceptable alternatives; `ordered` expresses an
explicit preference. A multi-node SkyPilot launch chooses a placement for that
cluster: it does **not** mean one node on every alternative provider. A request
for AWS **and** Nebius capacity needs separate named allocations to the same EKS
cluster. Never silently satisfy it with two AWS nodes.

The bundled task builder writes an ordinary SkyPilot capacity task without cloud
calls. Use it when preparing machine requests from an issue:

```sh
python /app/skills/skypilot/scripts/capacity_task.py \
  --name issue-123-capacity --gpu H100:1 --gpu A100-80GB:1 \
  --nodes 1 --disk-gb 100 --hold-seconds 900 > capacity.json
```

This requests either GPU and leaves cloud/region/machine choice to SkyPilot.
`--cloud aws --cloud nebius` restricts eligible providers without ranking them.
`--region` requires exactly one selected cloud. `--cpus` and `--memory-gb` express
minimums. The hold command gives the separate join/workload steps a finite window;
it is not a bill cap, cleanup confirmation or a workload completion signal.

The output is JSON, which SkyPilot accepts as YAML. On an authorized runtime with
its configured SkyPilot connection, review it using the installed version's
`sky launch --dryrun -c issue-123-capacity capacity.json`. A dry run's choice and
price are estimates; launch may encounter different availability. Execution must
stay within the approved provider/resource/runtime/cost constraints even during
SkyPilot fallback. Do not turn a review into an unbounded retry loop.

## Join, run and observe

Read [the hybrid sequence](references/eks-hybrid.md) for EKS-targeted requests.
SkyPilot allocates machines; WireGuard and nodeadm join them to the existing
workspace EKS; Kubernetes schedules the workload. SkyServe and a command run
directly on the VM do not prove that a requested EKS workload ran.

Keep the SkyPilot cluster/request identity, original EKS node/Pod IDs and issue/run
association. Repeated issue delivery should observe the existing allocation
before proposing a new one. After a lost launch reply, inspect the original
request; do not start a replacement simply because its reply was missing.

Report actual placement and workload output, not just Ready nodes. For mixed
providers, show a successful GPU workload on each. For serving, make a real
authenticated inference request. Preserve a service the issue asked to retain;
when cleanup is requested, verify owned resources rather than reporting success
from `sky down` submission alone. Distinguish estimates from provider bills.

## Current installation boundary

The maintained controller's launch path still accepts AWS-native EKS profiles
only. This skill and its builder restore agent-side resource planning; they do
not enable Nebius credentials, WireGuard execution or unrestricted launch access.
Use only the capabilities actually installed for the run. If the hybrid path is
unavailable, retain the prepared task and report that exact gap; do not downgrade
the user's request to AWS-only or bypass the controller's authorization boundary.
