# =============================================================================
# Isolated state backend — Issue #5836
# =============================================================================
# Declared as a PARTIAL configuration (empty block) on purpose: the bucket and,
# critically, the per-run state KEY are supplied at init time via
# `-backend-config`, so each fixture run gets its own state file:
#
#   key = fixture-edge/dev/<run_nonce>/terraform.tfstate
#
# Why isolation matters here rather than being tidy housekeeping:
#
#   * #5836 requires an isolated backend and forbids any ordinary platform apply.
#     Sharing the gateway's state would put a disposable per-run resource in the
#     same state file as production, where a `destroy` of the fixture is one
#     mistyped target away from the ordinary edge.
#   * Two concurrent fixture runs must not be able to corrupt each other. A
#     per-nonce key makes that structural.
#
# `terraform init -backend=false` still works for `terraform fmt`/`validate`/
# `test`, which is how the mocked tests run with no AWS account and no state.
#
# An operator who prefers no remote state for a disposable resource may init
# without these flags and keep state locally — but that local file contains the
# generated provenance secret in plain text and must not be committed. That is
# also why the secret is not a Terraform output. See RUNBOOK.md step 2.
# =============================================================================

terraform {
  backend "s3" {}
}
