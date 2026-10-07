# Optional chat sandbox warming

This operator-only path warms node slots and image layers, **not chat executors**. It never polls a queue or changes ingestion routing: every accepted turn still creates a new sandbox with its own projected workload token and disposable scratch space. The existing chat-agent image pre-pull is not sufficient because the model sandbox uses a different image. The webhook balloon reserves capacity in a different namespace for a different workload, so it is not reused here.

Warming is off unless an operator explicitly runs `configure-chat-warm.sh` with `CHAT_WARM_ACTION=enable`. The default action makes no cluster calls; there are no new Terraform resources or automated deployment triggers for this directory. Do not infer that opening or merging this PR authorizes a deployment. Before enabling, obtain the target and rollout approval required by the platform deployment procedure, check the active Kubernetes context, and confirm the separately isolated sandbox/supervisor installation is qualified. Do not replace the fresh sandbox path to improve benchmark results.

The script reads the sandbox image digest from the installed `adp-chat-supervisor-once` Job; it refuses an absent or unpinned digest. The optional DaemonSet caches that exact digest on nodes where it can schedule. A separate low-priority Deployment reserves 1–3 sandbox-sized slots and can be preempted by normal-priority sandboxes. Reservations have no sandbox labels, projected identity, gateway mounts, role or execution environment. When all reservations are used, missing or failed, the existing supervisor submits another fresh sandbox through normal cold scheduling; queue admission and the user-visible pending/starting lifecycle must remain active. This path does not promise a particular pickup latency or a schedulable node without an authorized live test.

After authorization and context verification, the operator may enable a bounded reservation:

```bash
CHAT_WARM_ACTION=enable CHAT_WARM_CAPACITY=1 modules/agent-factory/chat-warm/configure-chat-warm.sh
```

The command waits for both image-cache and reservation rollouts. Verify the intended image digest matches the supervisor Job, observe ready/desired pod counts and pending sandbox events, then execute the authorized A1/A2/B1 isolation and cold-capacity cases before admitting users. Measure cold, image-warm and node-warm first turns separately under the same model, prompts and concurrency. Record scheduling, image/bootstrap and model components, p50/p95 useful-output latency, failed attempts, idle node-hours and cost. A rollout success alone does not satisfy the latency or isolation gate; the common protocol is in `docs/architecture/adp-assistant-6929.md`.

To remove warm-only resources after authorization and context verification:

```bash
CHAT_WARM_ACTION=disable modules/agent-factory/chat-warm/configure-chat-warm.sh
```

The script waits for the reservation Deployment and cache DaemonSet to disappear, then removes their PriorityClass. It does not delete or modify assigned chat sandboxes, supervisors, queues or durable turns. A failed partial apply, rollout timeout or interrupted enable attempts the same cleanup. If cleanup reports an error, inspect warm-only resources and rerun `disable` with the approved context; do not assume the cluster is clean. If assigned turns are cancelled or interrupted, use the existing durable supervisor recovery flow rather than deleting their pods as part of warm cleanup. Record actual pod termination, pending-turn resolution and capacity cost in the authorized post-deployment evaluation; local fixtures cannot prove them.

## Disabled-path source validation

From the repository root, with Python 3.12+, Node.js, Terraform 1.7+ and `uv` installed, run:

```bash
uv run --no-project --with pytest --with pyyaml python -m pytest modules/agent-factory/tests/k8s/test_chat_warm_disabled.py modules/agent-factory/tests/k8s/test_chat_warm_plan_comparison.py -q
python3 modules/agent-factory/tests/k8s/compare_chat_warm_plans.py --base BASE_COMMIT
```

Replace `BASE_COMMIT` with the reviewed base commit available in local Git. The first command proves the default operator action emits no manifests and calls neither Kubernetes nor the renderer. The second compares resource changes and outputs from actual Terraform-root plans at that base and the current working tree. It uses variable defaults, not deployment tfvars, and shares the same provider lock between both plans. Provider installation needs registry access or a populated cache supplied with `--plugin-cache PATH`.

These plans run only in temporary directories with no inherited credentials, no backend initialization, mocked AWS/Kubernetes/Helm/archive providers and synthetic platform outputs. The sweeper bundle and archive hashes are synthetic plan-only inputs, never deployment artifacts. Expected result: identical default plans. This verifies source-level equivalence with warming disabled, not live state drift, built Lambda artifacts, IAM, sandbox isolation, latency or cost; the authorized #6937 owner must still qualify those live behaviors before enablement.
