#!/usr/bin/env bash
set -euo pipefail
# Run with Go 1.26.8 on PATH. Outputs matched, patched tools to ./bin.
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
work=${1:?usage: rebuild.sh BUILD_DIRECTORY}
mkdir -p "$work"
work=$(cd -- "$work" && pwd)
[[ $(go version) == 'go version go1.26.8 linux/amd64' ]]
mkdir -p "$work/bin" "$work/gcp" "$work/ssm" "$work/crc"
curl -fsSL https://codeload.github.com/kubernetes/cloud-provider-gcp/tar.gz/395432f6b23de7465d1a0ebb9b384cf96e3a04b3 -o "$work/gcp.tar.gz"
curl -fsSL https://codeload.github.com/aws/session-manager-plugin/tar.gz/dcff8da8cdecd53789bd06a193bd2032665dccd8 -o "$work/ssm.tar.gz"
curl -fsSL https://dl.google.com/dl/cloudsdk/channels/rapid/components/google-cloud-sdk-gcloud-crc32c-linux-x86_64-20260821073916.tar.gz -o "$work/crc.tar.gz"
printf '%s  %s\n' 3c22633cd3f2ebee3e73398d61ceb395055f140e54f8e9081ded54db156fb162 "$work/gcp.tar.gz" f75f8c3003045bbf5da7ee4dec7454cad65b5c9053d2516f3596ecb523c4cfed "$work/ssm.tar.gz" bbc5a0a1f93eec8996f166c7b014f63cf192ea17bd0fc55aa325e52d543a6a81 "$work/crc.tar.gz" | sha256sum -c -
tar -xzf "$work/gcp.tar.gz" --strip-components=1 -C "$work/gcp"
tar -xzf "$work/ssm.tar.gz" --strip-components=1 -C "$work/ssm"
tar -xzf "$work/crc.tar.gz" -C "$work/crc"
cp "$root/gcp-locks/"go.* "$work/gcp/"
(cd "$work/gcp"; GOWORK=off CGO_ENABLED=0 go build -buildvcs=false -mod=readonly -trimpath -ldflags '-X k8s.io/component-base/version.gitVersion=v35.0.1-gke.0-140-g395432f6b-adp-security1 -X k8s.io/component-base/version.gitCommit=395432f6b23de7465d1a0ebb9b384cf96e3a04b3' -o "$work/bin/gke-gcloud-auth-plugin" ./cmd/gke-gcloud-auth-plugin)
# AWS injects the release version before building; the source tag still says 1.3.0.0.
(cd "$work/ssm"; printf '1.2.814.0' > VERSION; make pre-build copy-src; GO111MODULE=off GOPATH="$work/ssm/build/private:$work/ssm/vendor" CGO_ENABLED=0 go build -trimpath -ldflags '-s -w' -o "$work/bin/session-manager-plugin" ./src/sessionmanagerplugin-main/main.go)
cp "$work/crc/bin/gcloud-crc32c" "$work/bin/"
