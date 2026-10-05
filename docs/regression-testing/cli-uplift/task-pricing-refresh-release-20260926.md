# Task pricing refresh — verified gateway maintenance release

PR #6342 fixes Tasks admitted with an earlier validated pricing version being refused when another gateway process has refreshed pricing. Only the pricing-evidence version may change: model, transport, policy, request shape and other bindings remain equal. Admission and current pricing evidence must both be present. Each operation still quotes current rates, verifies spendability and reserves against the original Task cap before provider handoff. Budget refusal prevents a provider call.

All final-head CI checks passed; the focused isolated model/binding suite passed 38 tests. The built source `0ed7116fb8dbb8c0399a9f8dd28edb22303db990` is the reviewed PR head and a verified ancestor of merged main. CodeBuild completed while CI ran; serving rollout began only after checks and merge.

- Target: account `000000000101`, dev, us-east-1.
- Build: `adp-dev-gateway-build:3bc0d266-4c9e-469a-8b5b-00e07f216818`.
- Source archive SHA-256: `a9e6ed5271c789d5a9bf2ec4fbbc9069cb50676d038ff4c847535d5c8c4e67ee`.
- Image: `000000000101.dkr.ecr.us-east-1.amazonaws.com/adp-gateway@sha256:245384f8d2a81055cccd8204c090a742e5276a9c33a94deb96cc5a82f2a48f9f`.
- Migration: `gateway-migrate-2b019eb2dc58`, completed before image change.

The uploaded source archive matches git archive byte-for-byte. ECR manifest/config digests and release environment match; packaged task_model.py, tenant/usage helpers and command manifest match reviewed source. The catalog receipt was merged in #6344 before regression dispatch.

The rollout changed only the gateway container image with resource-version and full-template guards. All seven live replicas are ready and their actual image IDs match. Both worker digest allowlists retain the same 13 entries. The scheduled engine uses the identical image and verify-only passes. All 35 served CLI file hashes match source; health returns 200. Worker image, service account, reduced IAM role and frontend were unchanged. Release state is recorded in an isolated checkout.

[Regression run 36224478998](https://github.com/aws-e/adp/actions/runs/36224478998) was dispatched after verification to test observed-running cancellation, replay/conflicting payloads, terminal stream and Activity, child exit and queue acknowledgement. Its outcome is separate from release verification; this release receipt does not claim successful cancellation or full CLI story acceptance.
