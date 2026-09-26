environment = "dev"
aws_region  = "us-east-1"
github_org  = "aws-e"
github_repo = "adp"
# Repo-level runner registration. Requires the dev GitHub App to have both:
#   Repository → Administration: Read and write  (register runner on this repo)
#   Repository → Actions:        Read and write  (claim and run workflow jobs)
runner_namespace = "arc-runners"

# Installation ID for aws-e-adp-agent-dev on the aws-e org.
# Refresh if the app is reinstalled:
#   gh api /orgs/aws-e/installations --jq '.installations[] | select(.app_slug=="aws-e-adp-agent-dev") | .id'
github_app_dev_installation_id = "124731131"

# Custom ARC runner image — constructed dynamically from caller identity at
# plan time. Override runner_image_repo/runner_image_tag if needed; leave
# runner_image empty to use the dynamic construction.
# runner_image_repo = "adp-arc-runner"
# runner_image_tag  = "latest"

# Gateway module is applied in dev — this flips on the terraform_remote_state
# read so the WebSocket $connect route can reuse the gateway's Cognito-JWT
# authorizer Lambda. When false, Terraform destroys the authorizer, which then
# fails with ConflictException because $connect still references it.
gateway_deployed = true

# Issue: agent-factory applies have been failing on aws_dynamodb_table_item.
# scaledjob_worker_agent — the canonical seed lives in gateway infra (#3085,
# lambda-authorizer module) and already wrote the item, this root's copy then
# fails CREATE with ConditionalCheckFailedException on every apply (the item
# type does not support terraform import, and ignore_changes only tolerates
# drift on a state-tracked item). Per the resource's own doc comment, pipeline
# deploys where gateway infra applies first should turn the duplicate seed off.
# Verified failing in apply run 33304480047 (2026-08-30); the repeated failed
# applies are also what left helm_release.arc_runner_set tainted and queued
# for destroy/recreate.
seed_agent_registry = false

# The ARC runner module (controller + scale set) is a live, load-bearing part
# of this environment — ALL CI jobs run on it. It was originally created by a
# manual apply that passed -var enable_github_apps=true, but that flag never
# made it into this var-file, so every pipeline apply since has PLANNED THE
# MODULE'S DESTRUCTION (count 0 → destroy) and only failed on the runner-set
# uninstall timeout. On 2026-08-30 two apply attempts (runs 33304480047 /
# 33305019328) pushed the destroy through and took down the CI listener.
# Declaring it here makes the pipeline's desired state match reality.
enable_github_apps = true

# Saved persona preferences resolve before dispatch; worker authority stays independent.
persona_model_mapping_enabled = true

# S14: dedicated identity for the label-triggered developer workflow. Provision
# this additive pool before routing that workflow to arc-runner-agent.
enable_agent_workflow_runner = true

# Existing gateway inline policies total 9,604 characters; intake needs 1,098.
# Use the same policy as a managed attachment, without changing worker IAM.
gateway_intake_managed_policy = true
