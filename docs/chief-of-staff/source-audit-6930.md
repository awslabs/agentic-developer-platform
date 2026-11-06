# Chief-of-Staff #6930: pinned source and claim matrix (D01)

This is a **repository source review**, not a deployment observation. The selected
architecture is [`825583373982de3e370f121544c4a18026454077`](https://github.com/aws-e/adp/blob/825583373982de3e370f121544c4a18026454077/docs/chief-of-staff/architecture.md),
whose document SHA-256 is
`258c31beb5447b384a151ab42d6b0ddb9672565555d57be2abe4cf80506ac8ae`.
The source snapshot reviewed below is the branch's initial
`4fbe153` revision. Hashes identify bytes, not deployed releases.

| Surface | Native source observation at the reviewed revision | Selected integration and ownership (not native proof) | Separate deployment evidence required |
|---|---|---|---|
| Owner identity and capabilities | [`chat_capability.py`](../../modules/gateway/src/agentauth/chat_capability.py) binds launch, user/tenant, run, session and scope to short-lived verification; [`chat_history.py`](../../modules/gateway/src/orchestration/chat_history.py) projects owner-scoped sessions. Client-supplied prompt/owner identity is not an authority. | #7198–#7200 and #7208 own the runtime/supervisor route; #7221 owns stable owner and generation across Crew targets. Preserve the gateway's independent membership and resource checks. | #6937 foundation isolation/identity receipt; #7217 and the staged live product reviews must check the actual deployed owner routes and versions. |
| Model transport | [`chat_model_provider.py`](../../modules/gateway/src/agentauth/chat_model_provider.py) invokes Bedrock Runtime through gateway-selected destination credentials, validates streaming and captures usage. It is not an AgentCore-memory adapter or native Crew model-provider proof. | #7210 owns gateway AgentCore memory and its selected region, strategy and delegated identity; #6935 owns the real provider prerequisite. | Inspect the deployed provider, region, streaming and identity and measure #7175 latency with the approved target; a manual Crew setup is not deployment evidence. |
| Existing chat data | [`chat_memory.py`](../../modules/gateway/src/agentauth/chat_memory.py) implements owner-partitioned DynamoDB memory with versions, ACL and TTL; [`gateway-memory.ts`](../../modules/agent-factory/agent/src/complex-task-chat/memory/gateway-memory.ts) calls gateway `memory/search`, `memory/read`, `memory/write`. These paths are **legacy scoped memory**, not portable AgentCore memory. | #7210 migrates the legacy store without silently widening scope or dropping deletion fences; #7202 separately owns Crew checkpoint metadata/API. | Compare cross-assistant recall, owner denial, correction/delete and migrated records on the actual deployed gateway and memory store. |
| Crew home and backups | The pinned architecture selects private-V2 EBS working state and independently uploaded S3 archive/manifest plus transactional `crew_checkpoints`; the inspected chat-memory and worker provider paths do not themselves implement this backup. | #7221 owns inventory/generation, #7201 Crew profile, #7202 backup/metadata, #7209 lifecycle and #7203 handoff. No Crew fork, replay plugin or S3 Files mount. | Check coherent capture, S3/DB partial-failure handling, stale-writer denial, restore and container–EC2 continuity with source/deployed version binding. |
| Deployment workflows | [`gateway-deploy.yml`](../../.github/workflows/gateway-deploy.yml) and [`chat-agent-deploy.yml`](../../.github/workflows/chat-agent-deploy.yml) expose guarded manual revision and named-dev CD paths; source presence is not a deployment receipt. | #7283 binds deployment handoff; #7368–#7372 own existing host preparation, not an invented host workflow. The runtime flow stays inert pending its actual gate. | Operator-supplied target, workflow definition and deployed image/schema receipts before #7217 or a staged live review; no engine dispatch or cloud authority follows from this audit. |
| Plan and qualification | [Registered memory revision](../delivery-flow/plans/cos-runtime-7197/revisions/2026-10-11-memory/submission.json) is v4; [product-review revision](../delivery-flow/plans/cos-runtime-7197/revisions/2026-10-11-product-reviews/submission.json) is v6. These are inert plans, not implementation. | #6930 reconciles the actual next base; #6937 retains single foundation-flow qualification ownership, and primary flow consumes dated evidence through its gate. | Preview and read back the saved revision; verify execution remains paused, acceptance unapproved and foundation receipts preserved. |

The paths above are bounded examples of inspected sources, not an exhaustive
absence proof. In particular, existing gateway memory and Bedrock transport do
not establish the selected AgentCore/Crew behavior; merged architecture and
registered plans do not establish a live service.

## Source SHA-256 register

Run `sha256sum` on the listed files at the reviewed revision to reproduce the
individual digests. The architecture digest above uses `git show` at the pinned
architecture revision, because this document changes as contracts are completed.

| Path (repository-relative) | SHA-256 |
|---|---|
| `modules/gateway/src/agentauth/chat_capability.py` | `a0c6473b6a71080aa7beecffd681df2d88bace4af68bf57170bf63be83641c9c` |
| `modules/gateway/src/orchestration/chat_history.py` | `f2ca8691f86cdbb240a4dd8a7a41e5091119a455866c8b47a212b6f21e12eb4e` |
| `modules/gateway/src/agentauth/chat_model_provider.py` | `110f188fc98e40a6f04492faed3b99cb5acab98e5eaec386cb02dd879f9d525c` |
| `modules/gateway/src/agentauth/chat_memory.py` | `f59b4fa438cd47dc9071741b3b82fe9c715a3f96329acbbd02e272b697df0096` |
| `modules/agent-factory/agent/src/complex-task-chat/memory/gateway-memory.ts` | `5db8091094d66f452fc60f2e16af2f19582074a4b44e00fca28f78d1c529f23a` |
| `.github/workflows/gateway-deploy.yml` | `8eb0c7682f80967bc02cfd60bdc8d171bd1f9466c51b642678cfe27898be36e7` |
| `.github/workflows/chat-agent-deploy.yml` | `ee040110cc5455737460616dfc20d0e74791cebdb93ef691f8fa2298bac056ec` |
| `docs/delivery-flow/plans/cos-runtime-7197/revisions/2026-10-11-memory/proposal.authored.json` | `6b8c6823854d0c6505ce4c799a77d37fee12e1ea8a65f494149ce7fa9b63dc62` |
| `docs/delivery-flow/plans/cos-runtime-7197/revisions/2026-10-11-product-reviews/submission.json` | `5bb7adf42932ec06e18df0058a0f4f32606527176915f20d05710fa0c97e1af4` |

Further contract decisions and negative walkthroughs remain pending D02–D04 and
ARC-01–ARC-07; this source matrix cannot accept those criteria by itself.
