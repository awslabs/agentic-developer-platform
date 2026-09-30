# Running cancellation and durable replay — 26 September 2026

[EC2 run 36224895411](https://github.com/aws-e/adp/actions/runs/36224895411) passed E42: **1 passed, 0 failed**. It was attempt 2 of evaluation `adp-e2e-20260926-064042-d484a8`, resumed from another maintenance session while this coordinator's resume waited in the workflow queue. The successful report and cleanup were independently verified afterward. Attempt 1 (36224478998) and later attempt 3 (36224953944) failed preflight during concurrent gateway rollouts; neither created another Task. Their failures remain recorded. The later failed attempt does not replace the successful attempt's evidence.

The served CLI observed Task `tsk_a35b35b7-c445-4cb1-94bd-ed6b47412a79` running, then `adp agent abort` accepted command `21a46f4b-e8f3-49af-ad29-a8ec0cede6ee` at 06:53:47 UTC. `adp task abort` recovered the same receipt, rejected a changed payload with `task_conflict`, and recovered the original receipt again. Terminal replay retained the same command identity.

The Task became cancelled with `cancelled_by_client`, confirmed child exit, confirmed native queue acknowledgement and no required recovery. The command receipt is cancelled with `handoff=not_started` and states that the process stopped before command consumption; this proves accepted cancellation and process termination, not runtime consumption of a steering message. Activity list and detail identify the same Task/invocation `e9d2dba0-e6c9-41d5-bc3e-085dc5c1130d`, cancelled state and available transcript.

The stream contained contiguous Task event cursors 1–7. Reconnecting after cursor 1 returned the exact six-event suffix through terminal cursor 7, without changed event data. The cancellation monitor exited 5 for the cancelled terminal outcome, as the harness expects; no completed-code or successful-inference outcome is inferred from that exit.

- Gateway source: `0ed7116fb8dbb8c0399a9f8dd28edb22303db990` (pricing refresh release), established by regression preflight.
- Worker pod: `agent-scaledjob-2pp8g-bnqq2`, UID `5d66c8ca-3a82-4b6d-81fd-4d1679af513a` from a strong-consistent Task workload binding.
- Worker image: `sha256:8a3af964d0a786a6c27b5e66d44ac32e8e451c23d375046663936d2007658db1`. CloudWatch Kubernetes metadata for that UID matches the image's ECR configuration digest `sha256:f9bef5ab259aff386816d9bf0686e3998490842242fbaff9b1934f981ab6c80c`.
- Existing protected service account and worker role were preserved. The concurrent reviewed Task SDK rollout added this image to the previous 13 allowed digests.
- One canonical model operation was confirmed, settled and usage-logged at verified ledger cost $0.006198. Independent complete MODEL/TURN reads reconciled only the unused $0.493802 hold; the original $0.50 reservation and all attempts remain recorded. The shared $5 qualification retains $0.930199.
- EC2 `i-0029a4f92f6dbc60c` was independently confirmed terminated. The test user's Task cap was restored from $0.50 to $0.25 by CAS version 4→5 and read back.

Artifact `10900706656` ZIP SHA-256: `9225d79f966a6d5785a13e8dfb3f379f2975c2a8ae8da5c2e45bdc1edd446639`. Downloaded archive digest and extracted report bytes were verified. Report SHA-256: `d1c66045e988a5007fbfcd6e19b43c8da565d672d2382014d1cc4a1fd698259d`.

This establishes running cancellation, durable replay/conflict, Task stream replay and Activity correlation. It does not close #5629: supported real pause/resume, steering, deeper foreign-owner controls and lost-acknowledgement/Ctrl-C acceptance remain separate. All original evaluation attempts and the shared $5 qualification ceiling are retained.
