#!/bin/bash
# Run as runner in the candidate. No remote cloud operations are performed.
set -euo pipefail
export AWS_EC2_METADATA_DISABLED=true
terraform version
helm version
docker --version
dockerd --version
containerd --version
runc --version
docker buildx version
aws --version
/kaniko/docker-credential-acr-env --help >/dev/null
work=$(mktemp -d)
trap 'rm -r "$work"' EXIT
cd "$work"
cat > main.tf <<'TF'
terraform { required_version = ">= 1.14.0, < 1.15.0" }
resource "terraform_data" "test" { input = "verified" }
output "result" { value = terraform_data.test.output }
TF
terraform init -backend=false -input=false
terraform validate
terraform plan -input=false -out=tf.plan
terraform apply -input=false tf.plan
test "$(terraform output -raw result)" = verified
helm create chart >/dev/null
helm lint chart
helm template local chart > rendered.yaml
test -s rendered.yaml
printf 'FROM scratch\nCOPY marker /marker\n' > Dockerfile
printf 'verified\n' > marker
sudo -n /kaniko/executor --context="$work" --dockerfile="$work/Dockerfile" \
  --destination=adp-local:fixture --no-push --tar-path="$work/image.tar"
test -s image.tar
aws sts get-caller-identity --generate-cli-skeleton input > skeleton.json
python3 -c 'import json; assert isinstance(json.load(open("skeleton.json")), dict)'
printf 'Terraform plan/apply, Helm render, Kaniko build, AWS CLI skeleton passed\n'
