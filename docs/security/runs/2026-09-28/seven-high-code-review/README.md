# Seven High Security Agent findings — source remediation

The 28 September review of `a1c0cf82` reported seven High findings. `findings.json`
binds each original finding ID to its repair and regression coverage. These are
source repairs, not a claim that the original AWS findings have been marked
resolved or that every deployed image is free of High findings.

The authenticated Zoekt image is published and pinned in `config.env`. Its
immutable digest, raw scan hashes and offline runtime acceptance are included.
The indexer is byte-identical to the previous image, preserving existing shard
compatibility. The frozen package database reports zero Critical and High.

## Rollout requirements

Deploy the new Door client and Zoekt image together through Agent Context Deploy.
The deploy script creates a dedicated random backend key once; only the Door and
Zoekt receive it. Existing keys are preserved across redeploys. The raw server is
loopback-only; public readiness discloses no index content. The authenticated proxy
exposes only `POST /api/search`. Missing or mismatched credentials deny searches.
NetworkPolicy is defense in depth; authentication does not depend on CNI enforcement.

IAM service subjects now use `iam-agent:<registry-id>`. Mutable `agent_name` is
only a display label. The worker identity and protected-budget gates use
`agent_registry_id`, retaining their security checks through the subject change.
External consumers that keyed service records on the old display-name subject
must migrate to the immutable ID; do not restore display-name-to-human aliases.

The dev sign-in configuration requires an existing platform organization
membership. Before applying that configuration, verify the membership projection
for legitimate operators and invited users. The broker refuses missing or
unavailable membership data. It does not fall back to open signup or the broken
OAuth organization-membership check.

The secret migration uses `removed` blocks with `destroy = false`, retaining
current out-of-band credentials and their stages while relinquishing Terraform's
placeholder versions. Fresh installs receive empty secret containers, not public
credentials. Secret containers retain a 30-day recovery window. GitHub App keys,
webhook credentials and GitLab credentials require coordinated provider-side
setup/rotation; this change neither rotates live credentials nor claims an
automatic rotation schedule. Existing webhook placeholders are rejected even
before infrastructure migration; marker verification already rejects them.

Skill-agent approval now requires the exact `/approve <plan-id>` command printed
on that plan. An authorized rejection is `/reject <plan-id> <feedback>`. The caller
must currently have repository write, maintain or admin permission, including on
the target repository when different from the issue repository. PM reassessment
commands have the same writer requirement and reject prefix/quoted lookalikes.

Live closure requires rollout acceptance and a fresh Security Agent review of the
merged source. No advisory was dismissed to make these source tests pass.
