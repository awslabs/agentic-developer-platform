# EKS hybrid capacity sequence

Source topology: `aws-innovate/AISuperPlane` at
`5d543c952493f0765133b92e93301b0b24d028ee`:

- `poc/eks-hybrid-skypilot/onboard-node.sh`: orchestrates launch and join.
- `infra/hybrid-node-prereqs/`: SSM/EKS access, Cilium, NVIDIA device plugin,
  WireGuard gateway and cluster configuration.
- `poc/eks-hybrid-skypilot/08-setup-wireguard.sh`: connects hybrid peers and
  forwards EKS-to-kubelet traffic through the gateway.

These are integration references. They are not bundled executable commands in
this skill, and their presence upstream does not mean the ADP hybrid path is
installed. Do not fetch and execute historical scripts to bypass missing runtime
capabilities. The supported provider/network executors must carry out the same
sequence under the run's existing authority.

## Allocation to workload

1. Bind the issue/run to the existing workspace EKS ARN, endpoint, CA and
   namespace. Reuse its networking and hybrid prerequisites when verified.
2. Express GPU requirements to SkyPilot. Unless constrained by the user or
   policy, leave cloud/region/instance selection to SkyPilot. For an explicit
   AWS-and-Nebius request, use two distinct named allocations targeting one EKS.
3. Record SkyPilot's actual instances and placement. Deliver hybrid activation
   and networking credentials through the installed private channel; task JSON,
   process arguments and issue logs must not contain activation codes or keys.
4. Establish the approved WireGuard peers and scoped routes, then finish nodeadm
   join and verify CNI/NVIDIA readiness. Preserve provider-local routes: historical
   #57's broad `10.0.0.0/8` WireGuard route broke communication between Nebius nodes.
5. Verify each original node's identity against the actual provider instance and
   the target EKS. Test EKS-to-kubelet reads, pod DNS and required pod/service
   traffic. WireGuard encrypts an overlay over public UDP endpoints; a successful
   handshake alone does not establish all required workload connectivity.
6. Submit the workload to EKS and observe its original Job/Deployment and Pod.
   Report which provider actually ran it. For a mixed-provider proof, run a GPU
   task on each; for one shared serving endpoint, verify real requests reached
   both backends.

Use the workload runtime and retain/cleanup intent recorded in the issue. A
SkyPilot capacity hold process ending does not terminate Kubernetes workloads
or prove provider cleanup. Shared gateways/routes and pre-existing capacity have
separate ownership from newly acquired machines. After uncertain replies, recover
the original resource identities before any retry or deletion.

The existing ADP executor already handles durable SkyPilot launch/status/down and
original workload ownership for AWS-native EKS. The remaining hybrid integration
must extend that path to external-node networking and credentials; this reference
does not claim it has been implemented or validated live.
