# Native lane IAM evidence

Source audit: HashiCorp Amazon plugin 1.8.2, commit
`3896533621ce21e5d8277b7e86e9bf02b577045a`. AWS action/resource/key support was
read from the official [EC2 service reference](https://servicereference.us-east-1.amazonaws.com/v1/ec2/ec2.json)
on 26 September 2026. The checked-in `ec2-authorization-reference.json` contains
the relevant action/resource excerpt and original document digest. This evidence
checks policy/request compatibility; only a real authorized build can establish
effective IAM/SCP/key-policy behavior in the selected account.

| Operation | Pinned request evidence | Enforced IAM scope |
| --- | --- | --- |
| CreateKeyPair | `builder/common/step_key_pair.go:70–92`, RunTags TagSpecifications when not restricted; passed at surrogate builder line 421 | Named native key namespace and RequestTag caller = current `aws:userid`. |
| RunInstances | `step_run_source_instance.go:129–140,181–211`; exact helper AMI/profile/type/block mappings; instance, interface and volume tags at creation | Exact helper image/network/source snapshot ARNs; exact type/profile/IMDSv2 on instance; caller request tag on new resources; no public IP on interface; existing key caller tag. |
| Stop/terminate helper | `step_stop_ebs_instance.go:39–63`; source-instance cleanup lines 482–496 | Current caller resource tag on instance. |
| ModifyInstanceAttribute | `step_modify_ebs_instance.go:54–65` sets helper ENA even when already enabled | Current caller resource tag and `ec2:Attribute=enaSupport`. No SG/profile mutation grant. |
| CreateSnapshot | `ebssurrogate/step_snapshot_volumes.go:69–83` includes snapshot TagSpecifications | Current caller tag on source volume; caller request tag on new snapshot, in separate resource statements. |
| RegisterImage | `ebssurrogate/step_register_ami.go:66–93`, no tags | Regional image wildcard because ID is allocated by AWS; **separate snapshot resource statement** requires current caller tag. Official reference lists `aws:ResourceTag/${TagKey}` for RegisterImage's snapshot resource. No request-tag condition on image. |
| CreateTags | Atomic creation authorization plus `step_create_tags.go:121–134` post-create | Atomic tags limited by `ec2:CreateAction`; later snapshot retag requires both existing/request caller tags. **No image CreateTags grant.** |
| Image tagging behavior | `ami_config.go:109–114` explicitly says no default Name tag; `tags.go:28` only converts input map; empty AMITags skips `step_create_tags.go:121` image request | Lane supplies empty AMI tags. Snapshot tags remain explicit. No implicit tagging privilege required. |
| DeleteKeyPair / DeleteSnapshot / DeleteVolume | key cleanup line 146; snapshot cleanup lines 134–148; volume cleanup lines 86–99 | Current caller resource tags. Explicit target launch mapping is excluded from volume cleanup; delete-on-termination is required. |
| DeregisterImage | Plugin can request it after registering an image during failed-build cleanup | **Not granted.** Failure retains the known/unknown AMI obligation for separately authorized operator review. |
| Describe APIs | Producer preflight/inventory and plugin discovery/waits | Explicit readonly action list, regional ceiling; AWS does not support resource scoping these discovery calls. |
| CodeBuild VPC ENIs | CodeBuild managed project attachment, outside Packer | Create only using dedicated supplied build subnets/SG; delete/permission by those subnets; permission only to CodeBuild service. These interfaces lack producer tags. |

The selected RegisterImage surrogate path uses RunInstances launch mappings; it does
not require independent CreateVolume, AttachVolume or DetachVolume. Explicit subnet,
security group and helper profile avoid their provisioning branches. No image copy,
sharing, IAM creation, role assumption, SSM registration/command or EKS permissions
are granted.

## IAM versus code boundaries

The native caller tag contains STS UserId, which matches the IAM `aws:userid` context
for the actual role session. The producer retrieves it from the active build role;
request/resource policies compare it to the authenticated context rather than an
arbitrary build identifier supplied by code. Different CodeBuild role sessions
cannot mutate each other's tagged helper resources or snapshots. Same-session
resources remain mutually reachable; unique build tags and exact original identity
checks provide the finer operation binding. This is a session boundary, not proof
that every request is the intended Packer request.

AMI creation necessarily has a regional image wildcard; source snapshot authorization
is caller-bound. Successful AMIs remain untagged and intentionally retained. Their
identity is the owned unique name plus exact snapshot and observed-volume provenance
recorded by the producer. A matching name is not deletion authority. No direct image
mutation is available after registration. Post-registration cleanup can therefore be
incomplete by design, and never reports clean success after that failure.

CodeBuild network-interface authority is scoped to the supplied **dedicated build
subnets**, not to each build. Do not reuse those subnets for unrelated detached ENIs.
The build role reads all versioned native inputs and can write evidence under the
lane's `builds/*` prefix, including later versions of prior builds' keys. S3 versions,
exact dispatch object/version pointers and external dispatcher receipts preserve
review evidence, but this is lane-level storage isolation, not per-build IAM storage
isolation. Native source inputs and dispatcher receipts are not writable by the build
role. Helper credentials cannot mutate either source or artifacts.

IAM does not enforce the target's selected KMS key or volume size through unsupported
RunInstances resource keys: the producer checks those concrete clone facts. The key
policy limits KMS authority to the selected key via EC2/S3; existing account EBS
settings and helper-root encryption must be compatible. No false claim is made that
an arbitrary malicious caller cannot request a different unencrypted launch layout
within its permitted resources. Only trusted reviewed source runs in this project.

The build boundary is an explicit action ceiling, regional EC2 limit and exact
non-EC2 resource ceiling; detailed EC2 resource/caller conditions live in the attached
identity policies. It is not a second copy of every identity condition. Policies are
split to respect AWS's 6144-byte managed-policy limit. Terraform tests check that
limit, exact helper PassRole, caller-bound snapshot registration, no image tag/delete
authority and private retained buckets.
