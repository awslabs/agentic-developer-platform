<!-- superplane-factory-roadmap:epic -->
## The problem in plain terms

Superplane users need recoverable workspace setup, verified training results and a clear current status. This epic implements the 15 October factory design in medium stories and provides migration from the current setup.

**The fix in one line:** Deliver account, networking, cluster and workload factories with durable recovery, selected-plan training and a verified cutover from the current installation.

## Description

- **Delivery owner:** ADP delivery-flow coordinator; assigns story and release/acceptance owners.
- **Completion boundary:** compatible releases deployed and all required ECs live-verified; merged children alone do not complete the epic.
- **Scope:** AWS-first factories/Argo, Azure/GCP/Nebius adapters, training/capacity/results, optional MLflow integration and existing-workspace migration.
- **Excluded:** tactical repair execution (#6970), automatic account closure, arbitrary provider support, live process migration and replacing Terraform with ACK/kro/Crossplane.
- **Product / initiative:** registered ADP Platform; Superplane umbrella https://github.com/aws-e/adp/issues/4910.

| Lineage | Value |
| --- | --- |
| Kind | epic |
| Project reference | ADP Platform / 470fb7f1-aa72-4544-a92d-b6cfedf1e761, verified 2026-10-26 |
| Parent epic | https://github.com/aws-e/adp/issues/4910 |
| Module / Feature IDs | Catalog aliases unmapped; coordinator resolves before dispatch; existing Superplane module is the implementation owner |

## Impact analysis

Preserve identity, approvals, budgets, attempts/fences, Terraform state and resource ownership. Prevent competing writers, false Ready and duplicate provisioning. The 2–3-day window is a scheduling target; acceptance and bounded execution policy still apply.

## Design

**End-to-end design:** [workspace and training architecture](https://github.com/aws-e/adp/blob/55c340f5c5b949bbdf4f9ac2c5646a868e4886d2/doc/superplane/architecture/README.md) · [controller contract](https://github.com/aws-e/adp/blob/55c340f5c5b949bbdf4f9ac2c5646a868e4886d2/doc/superplane/architecture/control-plane-and-reconciliation.md) · [complete refined backlog, dependencies and release handoff](https://github.com/aws-e/adp/blob/66651ea102099881823a669049d6f9bfc72e2470/docs/delivery-flow/plans/superplane-factories-20261026/README.md).

Parallel medium stories have explicit dependencies and three acceptance/scenario cases each. Serialize schema writers. Retain Terraform/ADP execution; Argo owns applications, execution owns jobs. Native sub-issues own hierarchy.

[Eight logical workstreams, every story and starting dependencies](https://github.com/aws-e/adp/blob/66651ea102099881823a669049d6f9bfc72e2470/docs/delivery-flow/plans/superplane-factories-20261026/workstreams.md).

| Logical group | Count | Native children |
| --- | ---: | --- |
| Shared foundation | 8 | C01 #8951, C02 #8952, C03 #8955, C04 #8958, C05 #8961, C06 #8959, C07 #8962, C08 #8953 |
| AWS accounts and networking | 5 | A01 #8966, A02 #8971, N01 #8972, N02 #8977, N03 #8978 |
| Clusters and application platform | 7 | K01 #8976, K02 #8982, W01 #8957, W02 #8987, W03 #8954, W04 #8992, K03 #8988 |
| Workspace UI and CLI | 2 | U01 #8964, U02 #8965 |
| Lifecycle and migration | 6 | L01 #8993, L02 #8994, M01 #8983, M02 #8996, M03 #8999, M04 #9000 |
| Training and capacity | 15 | T01 #8960, T02 #8963, T03 #8970, T04 #8956, T05 #8995, T06 #8997, T07 #9001, T08 #8998, T09 #9002, T10 #8975, T11 #9005, T12 #9008, T13 #9003, T14 #9006, T15 #9009 |
| Additional cloud providers | 14 | P01 #8967, P04 #8968, P07 #8969, P02 #8979, P05 #8980, P08 #8981, P03 #8989, P06 #8990, P09 #8991, P10 #8973, P11 #8974, P12 #8984, P13 #8985, P14 #8986 |
| Deployment and verification | 11 | R01 #9004, V01 #9010, R04 #9013, R02 #9012, V02 #9017, R03 #9007, V03 #9014, V04 #9015, V05 #9016, V06 #9018, V07 #9011 |

Reuse existing work without duplicate dispatch or implied legacy-epic closure. [Full EC-to-child AC mapping](https://github.com/aws-e/adp/blob/66651ea102099881823a669049d6f9bfc72e2470/docs/delivery-flow/plans/superplane-factories-20261026/acceptance-mapping.json).

## Deployment

- **R1 / R01:** one AWS/core/Argo/migration release; V01 verifies it. R04 cuts over the repaired existing workspace using those same artifacts.
- **R2 / R02:** one training/capacity release; V02 verifies it.
- **R3 / R03:** one additional-provider release; V03–V06 qualify providers independently.
- Implementation stories end at merged code/tests; release stories own builds, deployment and live evidence. Independent implementation is not blocked on each story's full rollout.
- Gateway continues automatic builds/rollouts. C08 validates actual serving Gateway/scheduler compatibility, retains concurrent changes and never freezes or downgrades shared Gateway.
- Use verified merge/release predecessor receipts, digest-pinned batch artifacts and maintained locks. Live targets, fixtures and spend limits are selected through existing authority before operations dispatch.
- Migration observes/adopts existing IDs and state, fences the old writer, activates one new owner and supports evaluated fallback. No cluster recreation. Current repair runs separately until cutover is ready.

## Validation

| ID | Epic outcome | Child AC / integration evidence | Phase/owner |
| --- | --- | --- | --- |
| EC-01 | Four factory layers preserve identity, authority, observations and restart-safe execution | https://github.com/aws-e/adp/issues/8953#AC-01; all contributing ACs in linked mapping | Compatible release; coordinator |
| EC-02 | AWS account/workspace creation reaches verified Ready and recovers without duplication | https://github.com/aws-e/adp/issues/9010#AC-01; all contributing ACs in linked mapping | Compatible release; coordinator |
| EC-03 | Drift, retirement, management restore and upgrades preserve owned/shared resources | https://github.com/aws-e/adp/issues/9000#AC-01; all contributing ACs in linked mapping | Compatible release; coordinator |
| EC-04 | The repaired existing workspace cuts over with stable identities and one active writer | https://github.com/aws-e/adp/issues/9013#AC-01; all contributing ACs in linked mapping | Compatible release; coordinator |
| EC-05 | Chosen training plans produce verified results, recover/cancel and release owned capacity | https://github.com/aws-e/adp/issues/9017#AC-01; all contributing ACs in linked mapping | Compatible release; coordinator |
| EC-06 | Qualified Azure environments and private AKS workspaces support lifecycle/recovery | https://github.com/aws-e/adp/issues/9014#AC-01; all contributing ACs in linked mapping | Compatible release; coordinator |
| EC-07 | Qualified GCP environments and private GKE workspaces support lifecycle/recovery | https://github.com/aws-e/adp/issues/9015#AC-01; all contributing ACs in linked mapping | Compatible release; coordinator |
| EC-08 | Qualified Nebius workspaces support private lifecycle/recovery with truthful capabilities | https://github.com/aws-e/adp/issues/9016#AC-01; all contributing ACs in linked mapping | Compatible release; coordinator |
| EC-09 | Three grouped releases remain compatible with changing Gateway; approved cross-provider continuation preserves lineage | https://github.com/aws-e/adp/issues/9018#AC-01; all contributing ACs in linked mapping | Compatible release; coordinator |

**Completion report:** cite every EC's child criteria, installed source/configuration, real provider/browser observations, recovery and survivor inventory. Planned/blocked/not-run checks remain incomplete. Filing these issues neither accepts a flow nor dispatches implementations.
