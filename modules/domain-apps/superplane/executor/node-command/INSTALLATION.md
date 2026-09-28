# Installing the native command capability

This is an installation prerequisite for the approved #5927 path, not an action
performed by the paid worker. It does not authorize deployment or image publication.
Use the existing installation/release authority and the selected account. Keep the
capability unavailable until the prepared image and effective role permissions have
been verified. The executor's image/document preflight is not an IAM simulation.

1. Build and review the prepared AMI and complete runtime manifest described in
   [README.md](README.md). Mask competing nodeadm boot units before first boot.
   Supply the exact reviewed SSM Agent version with ssmmessages support; legacy
   ec2messages fallback is not part of this permission set. Pin the actual GPU,
   Kubernetes, CNI and runtime artifacts. Record image IDs for every eligible region
   and the exact manifest digest. No AMI or compatibility evidence is supplied here.
2. Install the exact `bootstrap-document.json` and `probe-document.json` bytes as
   Command documents named `SuperplaneNativeBootstrapV1` and `SuperplaneNodeProbeV1`
   in each approved account/region, explicitly version 1. Installation owns document
   creation; the worker has no document mutation permission. Verify both returned
   document metadata and content hashes. A changed document requires a new reviewed
   implementation/version; do not update the default version behind an approval.
3. Render supplementary executor permissions with
   `superplane_executor.node_command_policy.executor_policy(account_id, regions,
   org_id=org_id, workspace_id=workspace_id)`. The instance allowance requires both
   service-owned `superplane-org` and `superplane-workspace` tags, added only by the
   approved native task. The executor independently verifies those tags and original
   launch membership before dispatch. Repeated allocations within that workspace
   need no per-allocation IAM update. Optional `capacity_names` restricts further to
   exact `Plan.cloud_cluster_name` values known before launch; no wildcard names
   are accepted. Install the reviewed workspace allowance through the existing
   role owner before execution; the worker never edits IAM.
   Use the already admitted provider role. Its existing trust, external ID,
   permission boundary, session policy and organization SCPs still apply.
4. Render `native_agent_policy(account_id, regions)` for the existing native EC2
   node instance role. It permits native registration and command channels, not
   SendCommand, hybrid activation or interactive session initiation. Preserve the
   established EC2 trust and EKS bootstrap permissions; this supplementary policy
   is not a replacement for either. Do not mount either role's credentials into
   tenant workloads or grant workspace members permission to assume the executor
   role or invoke these documents directly.
5. Verify effective permissions for the actual executor and native instance roles,
   including boundaries/SCPs/session restrictions. A policy fragment alone cannot
   establish effective permission. Check that generic command documents, another
   workspace's tags, foreign account/region, hybrid IDs, IAM changes and StartSession
   are unavailable through this capability. Other attached policies can widen a role;
   these allow fragments do not act as a permission boundary or remove existing grants.
   Review existing EC2 launch/CreateTags permissions and who can assume those roles:
   another tenant must not be able to forge or change the two scope tags. Absence of
   tag writes in this fragment alone does not establish tag integrity.
6. Verify private regional SSM and ssmmessages endpoint/DNS access and TCP443 return
   paths from the native node. Keep EKS API, pod/Service routing, registry pulls and
   controller-to-kubelet connectivity as separately verified network requirements.
   Run the authorized image/command/GPU acceptance with exact identities, deadlines,
   spend limits and cleanup evidence before claiming live support.

SendCommand uses both the exact custom-document ARN and an original EC2 instance
ARN. The role's instance allowance is constrained by the original organization and workspace
tags (and optionally exact `ray-cluster-name` values); the application checks original launch membership and live
instance identity before dispatch. One scalar organization/workspace pair is emitted per policy; the renderer never
combines independent lists of organizations and workspaces. IAM permits commands on
allocations within that installed workspace scope; the stricter original-allocation
and exact-instance checks remain with the authenticated application and journal.
This capability grants no tag mutation. Node
IDs supplied in an issue or found by label alone are not command authority.

GetCommandInvocation and DescribeInstanceInformation have no resource-level IAM
scope; their `Resource: "*"` permission is region-bounded and restricted to the
trusted executor. The API/application restricts which exact command handles it
reads. SSM message channels likewise require `Resource: "*"`; the initial control
channel and registration use native EC2 source-instance conditions. No wildcard
SSM document permission is emitted.

Permission resource/condition support was checked against AWS's machine-readable
service authorization reference v1.4 on 25 September 2026:

- https://servicereference.us-east-1.amazonaws.com/v1/ssm/ssm.json
- https://servicereference.us-east-1.amazonaws.com/v1/ssmmessages/ssmmessages.json

These sources confirm `ssm:resourceTag/${TagKey}` on SendCommand instance resources,
`ec2:SourceInstanceARN` on native UpdateInstanceInformation/CreateControlChannel,
and the absence of resource-level scopes on the four message-channel actions and
command-invocation/managed-instance inventory reads. Source/static policy checks
are not live IAM or prepared-image acceptance.
