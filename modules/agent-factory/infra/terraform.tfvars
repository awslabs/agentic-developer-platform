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
