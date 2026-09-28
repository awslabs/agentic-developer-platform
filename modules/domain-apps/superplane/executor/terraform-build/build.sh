#!/bin/sh
set -eu
# Keep the exact Terraform version required by workspace saved-plan consumers.
sha256sum go.mod go.sum > /tmp/terraform-module-hashes
CGO_ENABLED=0 go build -p 2 -mod=readonly -buildvcs=false -trimpath \
    -ldflags "-s -w -X github.com/hashicorp/terraform/version.dev=no" \
    -o /out/terraform .
sha256sum -c /tmp/terraform-module-hashes
