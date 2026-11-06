# The release root accepts the same private tfvars overlay as the bootstrap
# root (the deploy script merges one JSON document for both), so it declares
# the bootstrap-only inputs to keep that overlay valid. tflint would otherwise
# flag those declarations as unused and fail the plan check.
rule "terraform_unused_declarations" {
  enabled = false
}
