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
# THERE IS NO "JUST KEEP STATE LOCALLY" ALTERNATIVE
# -------------------------------------------------
# An earlier revision of this comment (and RUNBOOK step 2) told the operator they
# could "omit the -backend-config flags and keep state locally". That is FALSE and
# was reproduced as false on Terraform 1.15.3: with a `backend "s3"` block
# declared, omitting the flags does not fall back to local state, it FAILS init:
#
#   $ terraform init -input=false
#   Error: Missing Required Value
#     The attribute "key" is required by the backend.
#
# Believing the old comment costs an operator a confusing failure at the exact
# point where they are trying to stand up a disposable resource. The only two
# supported modes are therefore:
#
#   1. REAL RUN — S3 backend with every flag supplied, including a per-run key.
#      scripts/fixture-lifecycle.sh init does this and refuses to guess any of them.
#   2. CHECKS ONLY — `terraform init -backend=false`, which configures no state at
#      all and supports fmt/validate/test but NOT plan/apply.
#
# If genuinely local state is ever wanted, this block must be deleted, not
# under-configured — and note what that would mean: the local file would hold the
# generated provenance secret in plain text. That is a further reason the secret is
# not a Terraform output, and why mode 1 is the documented path.
# =============================================================================

terraform {
  backend "s3" {}
}
