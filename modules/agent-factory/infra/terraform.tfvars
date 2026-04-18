environment      = "dev"
aws_region       = "us-east-1"
account_id       = "879318057152"
github_org       = "aws-e"
github_repo      = "adp"
# Repo-level runner registration. Requires the dev GitHub App to have both:
#   Repository → Administration: Read and write  (register runner on this repo)
#   Repository → Actions:        Read and write  (claim and run workflow jobs)
runner_namespace = "arc-runners"

# Installation ID for aws-e-adp-agent-dev on the aws-e org.
# Refresh if the app is reinstalled:
#   gh api /orgs/aws-e/installations --jq '.installations[] | select(.app_slug=="aws-e-adp-agent-dev") | .id'
github_app_dev_installation_id = "124731131"
