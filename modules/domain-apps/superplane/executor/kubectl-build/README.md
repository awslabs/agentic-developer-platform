# Executor kubectl dependency rebuild

This rebuild retains the Kubernetes v1.31.4 command source at commit
`a78aa47129b8539636eb86a9d00e31b2720fe06b`, while replacing vulnerable Go and
library dependencies. `dependencies.patch` contains the complete reviewed module
change; the build never runs `go get` or resolves floating versions. The binary
reports `v1.31.4+adp.security.1` and a modified tree.

The workspace API still permits Kubernetes 1.31 through 1.35. Replacing its sole
1.31 client with 1.35 would lose supported skew for existing 1.31/1.32 inputs.
This patch preserves current client behavior; it does not repair the existing
client/server skew for newer targets or extend upstream support for 1.31.
A separate reviewed client-selection change is required for the full range.

Source archive: https://github.com/kubernetes/kubernetes/archive/refs/tags/v1.31.4.tar.gz
SHA-256: `ff3a2e9cae3b4734be4a6cdfeb933830c63219eaeda05cc4e59b7559fcde52b0`.
The final image retains upstream license notices alongside this build description.
