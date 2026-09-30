# Codex completion and Cognito regression — 26 September 2026

[Workflow 36220558371](https://github.com/aws-e/adp/actions/runs/36220558371) passed all three selected cases: E21 usage reads/exports, E42 hosted Codex completion, and D05 machine identity lifecycle. This partial evaluation is **3 passed, 0 failed**, not full Epic acceptance.

Gateway source `b2c3daf46e1d6724ba08239099e16d8fa43a001a` served 35 verified CLI files. Task `tsk_6011c464-98d4-4cb3-95f7-6cf7937c4819`, invocation `57e3ed64-0794-4df0-828f-565479f9ddac`, completed with process exit validated, no recovery required and queue acknowledgement confirmed. Actual worker image `sha256:5d3e952c1be21b1a2656b783fbebf0d653893469d9bc17e7f0ca623cef3f8572` ran using the protected worker service account and unchanged reduced role. The pod exited 0.

The runtime was Codex 0.157.0 using the Task compatibility transport. Task policy selected `global.anthropic.claude-haiku-4-5-20251001-v1:0`; this is not a GPT-model qualification. Five canonical settled model operations reported estimated cost $0.041662. Their conservative qualification hold remains separate from this reported estimate until independently reconciled.

Activity list/detail exposed the Task and retained report. The initial stream monitor timed out after four events; cursor replay after sequence 1 returned five events through terminal sequence 6. This proves replay, not an uninterrupted initial stream. The returned one-line tenant `--dry-run` help patch was applied exactly and independently reviewed in [#6333](https://github.com/aws-e/adp/pull/6333).

D05 passed six canonical-principal lifecycle checks and four Cognito checks: same-operation private credential delivery, ordinary/foreign read refusals, existing private output-file refusal, and retirement without credential redelivery. Owned client `7f26sl9ht34e6td8n3482gcsm2` and canonical principal `54ecbd9f-376d-4e56-804d-399b3b92ee84` were retired; aliases were revoked. This does not qualify IAM registration or actual session revocation.

E21's native-tenant CSV and NDJSON exports were empty (one page, zero records), so this run establishes empty serialization only. Separate isolated read probes in the authenticated aws-e membership observed two unique Task-linked records across two pages in each format, preserving owner/tenant and a pending continuation. [#6335](https://github.com/aws-e/adp/pull/6335) adds that explicit workspace fixture to the existing nightly scenario; populated EC2 qualification remains separate.

Cleanup completed and EC2 `i-00000000000000014` was independently confirmed terminated. Artifact ID `10899445446`, SHA-256 `5d075b083f3ea322f41ea03b135b4c85db6e8501d6e4e58af0b318ba62714172`; report SHA-256 `cf0b2a1a76aff8dced033835d47fe40f8fd68323df6bbd9992e42c69a7e8a060`.
