# Approved ordinary Pod network probe (#5927)

Authority: [Superplane DESIGN.md](../DESIGN.md), sections 2.2, 4.1 and networking.

An opt-in installed `network_probe` profile extension approves a single ordinary
batch workload, not an extra effect attached to existing workloads. The extension
names an existing ClusterIP Service's namespace, name, immutable UID, port and
bounded service-address CIDRs. No Service, secret or additional Pod is created.
The existing immutable workload image must contain the supplied probe module;
without an explicitly installed digest the profile cannot be admitted. This source
change supplies no published image digest and authorizes no image publication.

Preview derives a stable nonce from the fresh request UUID, organization and
workspace. It puts the entire versioned contract, including allocation and bound
cluster identity, into the batch invocation before calculating the existing plan
and destination digests. Replaying a request preserves its nonce; a new request
changes it. Existing profile shapes and admitted plans remain unchanged.

The executor verifies the current exact Service before launch/deploy and result
publication. The approved image resolves its Service DNS name and contacts only
addresses inside the approved CIDRs. It emits the bounded existing packet receipt
to stdout and the existing batch termination-result document. The executor binds
that receipt to the original completed ordinary Pod, reads its logs through the
existing namespace credential, and requires the log receipt to equal the retained
termination result. Service and Pod identities are reread before success.
Probe evidence requires `ClusterFirst` DNS, no host aliases and no custom DNS
configuration; injected hosts or resolvers cannot stand in for cluster DNS.
The Pod specification must also remain unchanged across the receipt read.
The actual probe Pod must retain the generated container image, invocation and
security settings. It cannot add volumes/mounts, environment overrides, working
directory overrides, lifecycle/probe hooks, init/ephemeral containers or alternate
termination channels. Such changes could replace the probe code or resolver while
leaving its image digest unchanged. The generated template already sets
`automountServiceAccountToken: false`; the probe needs no API token or projected
volume exemption. Normal Kubernetes scheduling metadata and image-pull references
remain permitted. These restrictions apply only to the explicit probe profile.

This proves this Pod's Service DNS/HTTP path and API-to-kubelet log access. It does
not prove node-side EKS traffic, CUDA execution, every cluster route, or a new
bootstrap transport. Those remain separate acceptance requirements. Cleanup,
budget, grants, fencing, original-UID ownership and results use the existing batch
lifecycle; no allocator, shared Gateway endpoint or tenant fleet permission is added.

Remote code-only tests must cover preview/replay/tampering, absent image digest,
foreign Service/namespace/UID, changed Service, failed DNS/HTTP, host-network Pod,
old nonce, mismatched result/log, missing output and revoked authority. Live
acceptance still requires authorized target, image release, budget and cleanup.

## Installed profile example and pending release

Add `network_probe` to an otherwise valid reviewed batch profile:

```json
{
  "version": 1,
  "namespace": "tenant-workspace",
  "service_name": "acceptance",
  "service_uid": "the-observed-immutable-service-uid",
  "port": 8080,
  "cidrs": ["172.20.1.4/32"]
}
```

Use workload command
`["python3", "-m", "superplane_executor.network_workload_probe"]` and empty
installed `args`. The preview's controller plan shows the generated contract;
its original request document keeps the installed empty args so retries match the
same caller request. Registration checks this reserved command's full generated
contract against the existing organization/workspace/allocation binding. Teardown
retains the original nonce and contract while using its own approved operation ID.

The profile's `workload.image` must be a reviewed registry digest containing the
probe source. Build source is `images/network-probe/Dockerfile` with executor as
build context and an explicitly reviewed `PYTHON_IMAGE` digest. No base/workload
image is selected, built, pushed or released by this change. Until the installed
workload digest exists and is retrievable, execution is unavailable.

Required additional permissions are namespace-only `get` on the approved Service
and `get` on the original Pod's `pods/log` subresource. No Secrets, Service writes,
Service proxy, pod exec or Node permissions are needed. Actual Pod `nodeName` to
provider-instance binding still needs the trusted cluster observer; Pod selector
metadata alone cannot prove that binding against admission-time nodeName injection.
