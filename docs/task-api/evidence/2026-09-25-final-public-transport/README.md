# Final public transport qualification

This directory binds the bounded-write gateway candidate to independent source review,
installed-module parity, and read-only public API observations. No new Task,
model operation, command or injected event is used. The existing held Task
`tsk_37ce236b-19a2-4d7c-a36d-b3609160b999` remains completed throughout.

At this observation, gateway and orchestration Lambda used image
`sha256:5e3a8b9e8b7900cd144e9924baf7a7fb63baa2534bbfda1249c5f09514c194a8`,
built from source `d8b8a793774f9390c0f28da2499dc9d541b6e369` with the durable
attempt-history and bounded SSE-write fixes. Eight gateway replicas were ready
and updated before the public probe began. Installed response/routes bytes match
those independently reviewed and tested; store/command bytes also match the
combined candidate source. AI-DLC remains paused.

The independent review executed 76 response, route and streaming tests. Its real
TCP fixture used the production 10-second write bound: a saturated reader closed
in 10.039 seconds while the controlled producer independently committed its
terminal event in 0.132 seconds. A disconnected relay released its stream, and
replay recovered all 2049 fixture events including completion. This local fixture
uses a controlled store; it is distinguished from the public observation.

The public probe retrieves the completed snapshot and verifies the artifact
against its advertised digest and structured report. It then holds one response
body unread for 20 seconds, closes that connection and replays exactly cursors
8 through 14 from cursor7. This small fixture does not saturate public TCP
buffers; it proves actual public disconnect/replay, while the installed-image
TCP fixture separately proves the write bound under real backpressure.

The probe next opens the completed Task after terminal cursor14, records idless
snapshot/comment heartbeat frames for the natural connection window, and
reconnects with the same durable cursor. Raw frames retain comment heartbeats;
they do not advance the cursor or count as authored progress. The earlier V4
held-task proof supplies actual authored progress and useful completion. Final
observed timings and outcome are in `public/result.json`.

The source review, native storage assessment, built-image TCP fixture and public
transport observations are complementary evidence. This directory does not
claim that a small completed Task reproduced public TCP saturation or that
heartbeats represent new agent work.

Deployment-wide stream quotas remain a separate known V3-06 gap: this candidate
uses per-replica counters. The shared Redis quota fix and cross-replica proof
are required before V3 closure. None of these passing transport observations
is presented as proof that per-replica counters enforce global limits.
