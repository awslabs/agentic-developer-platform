# URL researcher deployment — Embark1, 2026-09-23

The guarded AgentCore Browser collector is deployed in Embark1 account
`879318057152`, region `us-east-1`, on EKS cluster `adp-dev-eks-cluster`.
Live acceptance passed through the deployed worker image and service account.
Browser sessions are created on demand when the cyber agent requests a capture.
At final verification, the broker had two ready replicas and the image pre-pull
DaemonSet had 15 of 17 Pods ready on the new digest. All scheduled cache Pods
use the new image; the two remaining cache Pods were less than one minute old
and starting on new nodes while cluster autoscaling continued.

## Release and components

| Component | Deployed configuration |
| --- | --- |
| Browser broker | `adp-agents/url-analysis-browser-broker`, two replicas, container `browser-broker` |
| Broker entrypoint | `python3 /app/skills/url-analysis/browser_broker.py` |
| Broker endpoint | `http://url-analysis-browser-broker.adp-agents.svc.cluster.local:8765` |
| Browser identity | Service account `url-analysis-browser-broker-sa`; IAM role `adp-dev-url-analysis-browser-broker-role` |
| Reasoning runtime | KEDA ScaledJob `agent-scaledjob`, container `agent-worker`, service account `agent-scaledjob-sa` |
| Worker browser boundary | Inline policy `deny-direct-agentcore-browser` explicitly denies `bedrock-agentcore:*` |
| Warm pool | `agent-warm-pool` image updated; existing desired replica count of zero preserved |
| Image cache | `agent-image-prepull` DaemonSet updated to the same immutable image |

The broker and worker use:

```text
879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-agent-runtime@sha256:1a32e339c072f2dec9adabd7aa19617cf6fbef50de604a7bb6142138567d99d5
```

- Collector implementation: [PR #5782](https://github.com/aws-e/adp/pull/5782),
  source `16e5657e54e4ad070f2874d87a2d36697f6584b9`.
- Scoped worker deny policy and dev image pin:
  [PR #5784](https://github.com/aws-e/adp/pull/5784),
  merge `fdb140e2849c67599c518fb10851e87f2984e5c5`.
- Broker instrumentation opt-out:
  [PR #5788](https://github.com/aws-e/adp/pull/5788),
  merge `ab30f574a756bbaa2128cf143bcafec547b7e875`.
- [Runtime build 35845538360](https://github.com/aws-e/adp/actions/runs/35845538360)
  succeeded. GitHub job `107130671032` ran on
  `arc-runner-org-wxsd2-runner-rjn7p` with label `arc-runner-org`; it invoked the
  existing CodeBuild runtime build. URL regression CI also uses `arc-runner-org`.

All three release PRs are merged. The URL regression suite passed 243 tests;
the final worker/browser boundary suite passed six tests. PR #5788 passed all
seven reported CI checks before merge.

## Live acceptance

A temporary Kubernetes Job used the immutable runtime, the actual worker service
account and worker network-policy labels. Its validation entrypoint exercised the
maintained case API without invoking an LLM or posting messages.

| Check | Result |
| --- | --- |
| Worker identity | Account `879318057152`, role `adp-dev-agent-scaledjob-role` |
| Direct AgentCore access from worker | `AccessDeniedException` |
| `https://example.com/` | HTTP 200, complete observation, expected visible text |
| Controlled delayed JavaScript | Form absent initially and present after a three-second wait |
| Desktop/mobile comparison | Distinct expected headings; four complete observations across two sessions |
| Evidence integrity | Six example files and twelve synthetic-case files verified locally after download |
| Report rendering | All four synthetic screenshots loaded; report content visually inspected |
| Metadata-address request | `blocked_address` refusal |
| Durable storage | Worker uploaded the complete ZIP to S3 and verified exact byte-for-byte readback |
| Session cleanup | AWS independently reported all six acceptance sessions `TERMINATED` |
| Existing integrations | 33 credential version references and 36 installation mappings preserved |

The example case remains `inconclusive` because no assessment was supplied. The
controlled synthetic case received an evidence-linked
`no_adverse_behavior_observed` assessment about the known fixture. Neither result
measures threat-detection accuracy.

Evidence archive:

```text
s3://adp-dev-url-analysis-evidence-v2-879318057152/tenant=adp-default/issue=0/run=cyber-deploy-20260923/research-cases.zip
SHA-256: 9e1799c4589aaf984e0936cacc63022d1044fc36adc9c4b6f0d4e9ce3961bcef
```

Successful capture sessions:
`01M36WNDEAAPFFCVXD3Z3QP8P6`, `01M36WNGEGNBFFK4KFQ3NEF3AB`,
`01M36WNQEZDDF4ATQAAX2QMDAS`.
Earlier fixture attempts also terminated:
`01M36WAJDEZT2BXFD2GM46R082`, `01M36WAQ70M7ZFDHPEAKC9QWMD`,
`01M36WAYGVQHTYR84NVR4JRA4W`.

## Deployment findings and scope

Injected Node instrumentation stalled Playwright driver startup. A live comparison
isolated `NODE_OPTIONS` as the cause. PR #5788 disables CloudWatch/OpenTelemetry
language injection for the broker Pod template; corrected Pods have no injected
init containers or `NODE_OPTIONS`. Container logging remains enabled.

Account automation removed public access from the original temporary Lambda
fixture. The collector retained its HTTP 403 evidence as partial/inconclusive.
Acceptance then used harmless synthetic HTML returned by HTTPBin's stateless
base64 endpoint. Public Lambda permissions were not restored. The temporary
Lambda function, URL, permissionless IAM role, validation Job and ConfigMap were
removed, and the function log group is absent.

The operator prohibits making any Lambda publicly invocable, including temporary
fixtures. The deployment guide now records this restriction. The removed fixture
used `AuthType=NONE` and public invocation grants; do not reproduce that setup.
Function, URL, resource policy and execution-role absence were rechecked after
the operator raised this concern.

Only reviewed, saved broker/deny-policy plans were applied. Worker, warm-pool and
pre-pull image fields were updated while preserving their other settings and
existing running worker jobs. The separate protected-worker migration hold and
activation flags remain unchanged. Existing GitHub credentials, installation
mappings and broker settings passed the preservation check after rollout.

This validates managed browser collection and evidence storage. It does not claim
an LLM-driven UI/GitHub end-to-end run, automatic case publication by the case CLI,
or representative threat-corpus accuracy. Very long target URLs can overflow the
HTML report header; follow-up `adp-g60` tracks that presentation issue. The case
data, observation panels, images and integrity checks remain usable.

## Researcher test

In a new cyber-agent run, request:

> Analyze https://example.com/ with the URL-analysis skill. Capture an initial
> view and a view after three seconds, compare mobile if useful, verify the case,
> and publish the complete evidence bundle. Cite observations and explain limits.

The [researcher guide](url-researcher-cases.md) includes direct runtime commands,
artifact descriptions and collection limits. Existing jobs retain their original
image; a new worker run uses the deployed digest.
