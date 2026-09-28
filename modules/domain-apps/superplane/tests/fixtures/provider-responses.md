# U11 provider-response fixtures

`provider-responses.json` supplies the recorded provider responses the
handle-and-reconciliation suite reconciles against
([#5049](https://github.com/aws-e/adp/issues/5049), EPIC
[#4910](https://github.com/aws-e/adp/issues/4910)). It is **generated**, by
`generate-provider-responses.py` in this directory, and regenerating it is the
whole point of it being a generator rather than a file somebody typed.

## Why generated

The story's rule is that these fixtures must be captured real responses or derived
from the provider SDK's response models, and never written from the adapter's
expected shape — otherwise "adapter and fixture become self-consistently wrong
about a timeout". That failure is undetectable by review: a fixture written from
`adapter.py` and a fixture written from the SDK look the same on the page. It is
detectable by a generator, which reads the producing definitions and exits
non-zero when they no longer declare a field the fixture uses.

So no key in the JSON is typed in the generator. They are read from:

| Section | Derived from | How |
|---|---|---|
| `skypilot` | the pinned snapshot's `skypilot/types.go` | `json:"..."` struct tags and the `ClusterStatus` constants, parsed out of the Go source |
| `ec2` | `botocore`'s service model for `ec2:DescribeInstances` | the same JSON model boto3 unmarshals with at runtime, including the `InstanceState.Name` enum |

The struct tags are the wire contract rather than a description of it: they are
what the Go client actually unmarshals SkyPilot's responses with.

## Provenance

SkyPilot, at upstream revision `5d543c952493f0765133b92e93301b0b24d028ee`
(the snapshot pinned by `spike/provenance.py`, held on the planning branch and
deliberately not copied into `main`):

- Path: `modules/domain-apps/ai-super-plane/reference/src/superplane-controller/skypilot/types.go`
- Git blob SHA-1: `99e07955a0b20dc488ed4a630824d5962e452857`
- Structs read: `RequestResponse`, `ClusterInfo`, `ClusterHandle`,
  `LaunchedResources`; constants read: `ClusterStatusInit`, `ClusterStatusUp`,
  `ClusterStatusStopped`.

EC2: `botocore` 1.43.95's bundled `ec2` service model, `DescribeInstances`
output shape → `Reservations[].Instances[].State.{Code,Name}`.

## What the values are, and are not

Identifiers are **synthetic** — `sky-node-a1b2c3`, `i-0abc1234def567890`,
account `000000000000`, RFC 4122 zero-padded request ids. This is not a captured
live response, contains no credential, and asserts nothing about any deployed
environment. `autostop: 120` mirrors `DefaultIdleMinutesToAutostop` at
`onboarder.go:20-21` because the idle-node cost leak R15 also names is a property
of that default, not because any environment was observed using it.

## Which case each entry is for

| Entry | The situation it records |
|---|---|
| `skypilot.launch_accepted` | the `POST /launch` response *when it arrives* — the one the timeout branch never sees |
| `skypilot.status_present_up` | the re-check after a lost launch response: capacity exists, so a repeat would duplicate spend |
| `skypilot.status_present_init` | still coming up. `PRESENT`, not absent — `INIT` bills |
| `skypilot.status_absent` | `POST /status` returns `[]` for an unknown cluster: provider-established absence, the only route to `RETRY_PERMITTED` |
| `ec2.describe_instances_running` | the allocation is up whatever a local status field says |
| `ec2.describe_instances_shutting_down` | mid-teardown: the state that makes "terminate returned, therefore terminated" wrong |
| `ec2.describe_instances_terminated` | absence the provider itself established |
| `ec2.describe_instances_empty` | the other form of absence: no reservations at all |

There is deliberately **no fixture for the ambiguous case itself**, because a lost
response is the absence of a response. The suite models it as a raised
`TimeoutError`, and the fixtures supply what the *re-check* returns afterwards.

## Reproducing

From the repository root, with the gateway's Python dependencies installed:

```bash
git fetch origin agent/issue-4910
python3 modules/domain-apps/superplane/tests/fixtures/generate-provider-responses.py
```

It writes `provider-responses.json` and prints nothing on success. A diff means a
producing model changed; review it before committing, because that is exactly the
signal the generator exists to raise.
