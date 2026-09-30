# Codex cancellation qualification — 26 September 2026

Workflow [36221032703](https://github.com/aws-e/adp/actions/runs/36221032703), evaluation `adp-e2e-20260926-053105-471cc5`, failed E42: **0 passed, 1 failed**. This is retained failure evidence, not running-cancellation acceptance.

The installed gateway/CLI release was `b2c3daf46e1d6724ba08239099e16d8fa43a001a`. The harness observed `running` for Task `tsk_107b4d2a-80e4-4a53-9b7f-2488d4b24938` (invocation `9ba608b6-3b94-485e-9290-381d718b5f32`). Initial `adp agent abort` returned exit 5 rather than the expected pending exit 4. A repeat of the exact command ID and reason confirmed HTTP 400. The harness had generated UUIDv5 command `99a59f56-95ad-5385-80fe-ba9e499fb403`; the canonical Task command schema requires UUIDv4. Cleanup used the same invalid UUID version.

The Task subsequently completed with no command receipts and queue acknowledgement confirmed. EC2 `i-07b6ba449c104bd15` was independently confirmed terminated. The workflow reports cleanup complete. There is no cancellation, payload-conflict, child-exit-on-cancellation or terminal-cancellation-replay acceptance from this run.

The recovery-plan fix derives stable UUID4-shaped command identifiers from the existing evaluation/tenant/principal/persona identity and separate control/cleanup purposes. They are deterministic operation identities, not random credentials. Plan schema v2 preserves the original request identity but rejects an old retained plan comparison; existing failed plans and Task records must not be rewritten or automatically replaced. Regression checks require canonical UUID4 shape, stability, separation and changed-evaluation distinction.

The temporary human Task policy cap was restored from $0.50 to $0.25 using expected version 2; fresh readback confirms version 3 and unchanged other policy fields. The shared $5 qualification ledger retains $4.980319 of spend and conservative reservations; no further paid dispatch was made.

Artifact ID `10898542984`, SHA-256 `1e7a9b7c294c4e7fc36dc714b505332b268a95b1bab175d4a20387926c2944e4`. Private Task reports, authentication and transcript content are not published here.
