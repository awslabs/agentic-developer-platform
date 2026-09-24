# Superplane agent skill source

The original `src/superplane-skill/SKILL.md` described a separate Superplane CLI
and login. The maintained skill uses the actual `adp superplane` parser in
`modules/gateway/cli/adp-superplane.py` and its onboarding helper. Old nodepool,
job-submit and serving-test examples are removed because those CLI verbs are not
implemented there. This does not remove the underlying HTTP/UI workload routes.

GPU resource planning is restored through the sibling `skypilot` skill, based on
`.claude/skills/skypilot/SKILL.md` in aws-innovate/AISuperPlane at
`5d543c952493f0765133b92e93301b0b24d028ee`. Its task builder preserves accelerator
and permitted-provider alternatives for SkyPilot to optimize. The EKS reference
records the original WireGuard/nodeadm sequence and the current execution gap.

Both skills are copied by the existing domain skill staging mechanism. Source
and staging tests do not prove a deployed worker contains them. There is no new
agent framework, login mechanism, allocator or live runtime activation here.
