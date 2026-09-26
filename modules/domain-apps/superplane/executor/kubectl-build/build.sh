#!/bin/sh
set -eu
# Keep the supported CLI minor behavior; report the downstream dependency patch.
version=v1.31.4+adp.security.1
commit=a78aa47129b8539636eb86a9d00e31b2720fe06b
flags='-s -w'
for package in k8s.io/client-go/pkg/version k8s.io/component-base/version; do
    flags="$flags -X $package.gitVersion=$version -X $package.gitMajor=1 -X $package.gitMinor=31"
    flags="$flags -X $package.gitCommit=$commit -X $package.gitTreeState=dirty"
done
CGO_ENABLED=0 GOWORK=off go build -p 2 -mod=readonly -buildvcs=false -trimpath \
    -ldflags "$flags" -o /out/kubectl ./cmd/kubectl
