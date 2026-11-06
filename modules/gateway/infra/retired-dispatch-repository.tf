# #8650 moved routing to accepted repository bindings. An upgrade's first
# infrastructure pass runs before gateway/schema cutover, so deleting this
# SecureString would remove a dependency of the still-serving old gateway.
# Retain its encrypted value for old readers and legacy migration evidence,
# regardless of whether it is populated or disabled. This state-only removal
# creates nothing on fresh installs and does not restore global routing inputs.
# Binding attribution remains subject to the stored-provenance checks in
# docs/runbooks/story-bound-repository-rollback.md; this is not their clearance.
removed {
  from = aws_ssm_parameter.orchestration_dispatch_repo

  lifecycle {
    destroy = false
  }
}
