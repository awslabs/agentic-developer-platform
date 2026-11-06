# Task model qualification

The dedicated `persona-model-probe` identity can opt into one Task Messages or Responses
profile by setting `ADP_TASK_PROBE_PERSONA` to one of:

- `agent-task-investigator` (16 output tokens)
- `agent-task-cyber` (64 output tokens)
- `agent-task-claude-developer` (64 output tokens)
- `agent-task-codex-developer` (64 output tokens; legacy Messages compatibility)
- `agent-task-gpt-intent-refinement` (64 output tokens; native Responses)
- `agent-task-gpt-developer` (128 output tokens; native namespaced Responses tools)

The existing local `ADP_PERSONA_MODEL_PROBE_ENABLED=true` gate and server probe
admission must both be enabled. Each opted-in invocation claims at most one
slot; the server selects its model from the existing model allowlist and its
verified platform destination. Customer destinations remain excluded.

Without this opt-in the worker runs its existing Claude SDK probe cycle. Older
workers send no `task_persona` claim and cannot consume Task profile slots.
Task and legacy claims share exactly the existing daily cycle, slot count,
reserved spend and started spend. No budgets or daily manifest fingerprint are
reset by introducing Task profiles.

The checked-in `task-profiles.json` mirrors `src/tasks/personas.py`; gateway
parity tests require exact body, revision and SHA-256 matches. The worker checks
the entire body before durable start, performs one Bedrock InvokeModel call or one SigV4-signed Mantle Responses call
with retries and redirects disabled, and requires the expected text/tool response and a
provider request ID before reporting proven evidence. A lost start receipt or
uncertain provider result is never retried automatically.

Responses calls bind the claimed model and region, request nonstreaming output,
disable storage and request encrypted reasoning. Responses bodies are streamed
into a 64 KiB bound. Completed text must be exactly the OK sentinel; completed
tool evidence must contain exactly the admitted `mcp__adp.task_probe` call with
`{"value":"OK"}`. A probe never executes the returned tool. Codex Task claims
select only Codex models; Messages claims continue selecting Claude models.

This qualifies the bounded provider contract used by each Task persona. It
does not claim to prove the complete coding/Codex runtime, repository tools or
remote-control acceptance; those require the separate Task integration and
live remote-control scenarios.

The native Responses path is covered by local transport/admission tests. It has
not been deployed or qualified against a live admitted probe slot. Existing
probe enablement, account/spend approval and rollout gates remain unchanged.

## Claude request-shape manifest (`request-shape-manifest.json`)

The Claude SDK probe (`runner.ts`) only forwards a Bedrock request whose
normalized body digest equals the gateway's expected digest for the model. Both
sides read the same checked-in manifest: this directory's copy and
`modules/gateway/src/admin/persona_models/request-shape-manifest.json` must be
byte-identical (`manifest-parity.test.ts`, the gateway's
`test_gateway_manifest_is_the_sdk_generated_artifact`, and the
`Claude Request-Shape Manifest Drift` job in `agent-worker-image.yml`).

`canonical-json.ts` (`claude-code-probe-context-v5`, #7993) removes the context
Claude Code injects per run before hashing: the random device id, the initial
`USD budget: $0/$B; $B remaining` reminder (the gateway's per-attempt budget),
the wall-clock date, and the `Shell:` / `OS Version:` lines of the SDK's
`# Environment` block (the pod's or developer machine's shell and kernel). Model,
tool declarations, token and thinking limits, prompts, platform, and account /
session identity remain covered. Each rule fires for exactly one occurrence.

### Regenerate from the runtime image (the only accepted source)

Generate from the image the probe CronJob runs, never from a developer machine,
and commit the output to both paths. Replace `<digest>` with the `agent_image`
digest pinned in `environments/<env>/modules/webhook-ingress.tfvars` (built from
the commit whose `canonical-json.ts` the gateway will run):

```bash
export KUBECONFIG=~/.kube/config-adp-dev
NAME="manifest-gen-$(date -u +%H%M%S)"
IMAGE="879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-agent-runtime@sha256:<digest>"
kubectl -n adp-agents apply -f - <<YAML
apiVersion: batch/v1
kind: Job
metadata: {name: ${NAME}, namespace: adp-agents, labels: {app.kubernetes.io/name: agent-scaledjob, app.kubernetes.io/component: manifest-generation}}
spec:
  backoffLimit: 0
  ttlSecondsAfterFinished: 1800
  template:
    metadata: {labels: {app.kubernetes.io/name: agent-scaledjob, app.kubernetes.io/component: manifest-generation}}
    spec:
      restartPolicy: Never
      serviceAccountName: persona-model-probe-sa
      automountServiceAccountToken: false
      containers:
      - name: gen
        image: ${IMAGE}
        command: ["node", "/app/dist/invocability-probe/generate-manifest.js"]
        env: [{name: AWS_REGION, value: us-east-1}, {name: HOME, value: /tmp}]
        resources: {requests: {cpu: "1", memory: 2Gi}, limits: {memory: 4Gi}}
        securityContext: {allowPrivilegeEscalation: false}
YAML
kubectl -n adp-agents wait --for=condition=complete --timeout=600s "job/${NAME}"
kubectl -n adp-agents logs "job/${NAME}" | grep -v '^\[manifest' \
  > modules/agent-factory/agent/src/invocability-probe/request-shape-manifest.json
cp -f modules/agent-factory/agent/src/invocability-probe/request-shape-manifest.json \
      modules/gateway/src/admin/persona_models/request-shape-manifest.json
```

`probeSdkEnvironment` drops `CLAUDECODE`/`CLAUDE_*` inherited from a shell that
runs inside a Claude Code session and `GITHUB_ACTIONS` from CI runners before
applying the pinned probe settings: the first changes the SDK billing header
(`cc_entrypoint`), the second makes the CLI classify itself as `github-action`
and append the option-preview paragraph to the `AskUserQuestion` tool
description. Tool declarations are semantic and are never normalized, so these
hosts are made to look like the probe pod instead.

The `app.kubernetes.io/name: agent-scaledjob` label is required: `adp-agents`
has a default-deny egress policy and the generator only needs loopback, but the
pod must still be scheduled like the probe. The generator refuses to emit a
manifest when a model's digest differs between its two passes (budgets 0.01 and
0.5), which is the in-image proof that the budget normalization fires. `npm run
generate:invocability-manifest` (ts-node, loopback only) must produce the same
bytes from any machine; CI fails on drift. If the image does not yet contain a
normalization change, the digests it prints are stale: build the image from the
merge commit first, regenerate, then bump the CronJob pin.

### Diagnosing `request_shape_mismatch`

Set `ADP_PROBE_PRINT_BODY=1` on a one-off probe Job (copy the CronJob template
and add the variable; never on the CronJob itself) to make the runner print the
canonical, normalized body of a refused request to stderr in numbered chunks
`[invocability-probe:body:<model>:<n>/<total>] …`; the generator prints
`[manifest-body:<model>:<n>/<total>] …` under the same switch. Reassemble and
diff the two bodies:

```bash
kubectl -n adp-agents logs job/<job> \
  | grep -o '^\[invocability-probe:body:[^]]*\] .*' | sed 's/^[^]]*\] //' | tr -d '\n' > probe-body.json
```

The body carries prompts, tool schemas and model parameters only; SigV4
credentials travel in request headers and are never printed.
