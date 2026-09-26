# Token-refresher entrypoint fixtures (#6111)

Run `python3 -m unittest discover -s modules/agent-factory/docker/github-token-refresher/tests -v`
from the repository root. The suite runs the shipped Bash entrypoint, signs a
fresh disposable RSA key with real OpenSSL, and uses local AWS/GitHub provider
executables. It inherits no credentials or user configuration. It does not
validate GitHub's live JWT acceptance, provider pagination, or production rollout.

Nine tests exercise exact token bytes and private permissions, missing and
unmatched owner refusal, duplicate/invalid installation refusal, malformed token
responses, secret and signing failures, conditional sidecar refresh failure,
and failed atomic replacement. Four different sidecar failures preserve the last
good token. Temporary signing and publication files are cleaned on failure.
The original script at `30b21fe4e` fails 10 assertions in the initial eight-test
version: logs contaminate tokens, malformed responses overwrite good tokens,
sidecar failures overwrite good tokens, signing failures continue, and invalid
installation responses are accepted. The additional ninth test covers failed
atomic publication.

Build and test the actual image, using the immutable image ID returned by the
build in place of `<image-id>`:

```sh
docker build -t token-refresher-fixture modules/agent-factory/docker/github-token-refresher
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges \
  --tmpfs /tmp:rw,exec,nosuid,nodev,size=64m \
  -e TOKEN_REFRESHER_SCRIPT=/usr/local/bin/github-app-token.sh \
  --mount type=bind,src=/absolute/checkout/modules/agent-factory/docker/github-token-refresher/tests,dst=/fixture,readonly \
  --entrypoint python3 <image-id> -m unittest discover -s /fixture -v
```

The temporary filesystem must allow execution of the disposable provider
executables. No host auth/config directories are mounted; networking is disabled.
The image's default non-root user is retained.

2026-09-26 local acceptance: all nine tests passed on the host and in the full
image as UID/GID999, Python3.9.25 and OpenSSL3.5.8. The tested OCI index was
`sha256:9b9be972f54bd8216e970504ab757a931676b4f22e1e1e287cff51bbed778d40`;
its build-reported Docker config was
`sha256:7b69086ab271aa25c7a3fde3393b12d01a5d6bc6393dd129317b2dd799f30236`.
The installed script and checkout both hash to
`06e632a32faa769254cbaa705df2d9a288d2a07ed36733429ae1ef40e7b73f4e`.
ShellCheck and Bash syntax validation pass. Credential Path CI runs this suite.

This closes the token-refresher fixture gap only. All original #6111 image
findings remain retained; four-image vulnerability reconciliation, other image
runtime acceptance, and any publication/rollout are still required. No image
vulnerability clearance or live acceptance is inferred from this test.
