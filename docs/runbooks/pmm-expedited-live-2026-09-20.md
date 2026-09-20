# Expedited PMM rollout — dev, 2026-09-20

The operator requested PMM live without the seven-day soak on 2026-09-20.
Target: account `879318057152`, region `us-east-1`, AWS profile `embark1`.
This supersedes the elapsed-time prerequisite in the earlier dated PMM readiness
report for this dev rollout. Profiles remain deferred.

Use immediate, bounded live verification of human ownership, per-persona model
selection, delegation, AI-DLC/replan and rollback before declaring the feature
live. Preserve accurate telemetry: absent observations remain absent, and the
credential-binding gate must not report a seven-day soak that did not happen.
The global credential-binding rollout remains separately tracked by #3186.

The shortened schedule does not remove runtime requirements. Install compatible
gateway/worker images and their authenticated authority/signing prerequisites;
verify actual provider model receipts and rollback. Do not certify success from
the UI flag alone or from report-only proposals. Keep background probes disabled
unless separately configured within the agreed test budget. Customer-linked
accounts are outside this rollout's verification scope.

The user has authorized deployment. A total model-spend ceiling has been
requested and is pending; no paid test invocation is authorized by an assumed
ceiling. Record the ceiling before running the paid canaries.

## Deployment record

- Replan ownership fix #5558 merged as
  `1a5ff3e8368906270c1a7c87861a9ef164c504f3`.
- Readiness evidence repair #5570 merged as
  `7381d64db992133a3fb30883329253f4c4ebfb18`.
- Both PRs passed CI before merge. Existing gateway deployment workflows publish
  the merged code. Verify deployed images and health before activation.
- The UX change is included in this rollout branch; its existing offline
  frontend, CLI and worker checks passed in the prior verification candidate.
- Infrastructure is being planned against the existing state using retained
  upgrade inputs. No broad webhook apply or elapsed-time gate bypass is implied.
