# Bounded Transform qualification image inputs

These recipes prepare the provider base and the separate offline test base for
the disposable archive-receipts and queue-worker fixtures. They establish image
inputs only; no native result, image digest, provider qualification or successful
delivery is asserted by these files.

`Dockerfile.base` combines exact linux/amd64 manifests for Python 3.12.16 (with
Git) and Node 22.23.4. It copies only Node's executable and checks the toolchain.
There is no package-manager update or mutable base tag. Its published digest
must come from an actual reviewed build and independently verified ECR receipt.
The existing unproven base tag is not interchangeable with this recipe.
Both bases default to numeric user `10000:10000`. Derived executor recipes
explicitly use root only for installation, then restore that non-root user.

For `Dockerfile.check-base`, prepare a private build directory containing that
Dockerfile, `requirements.lock`, `wheelhouse.sha256`, the dependency selector,
and a `wheelhouse/` directory with the exact six wheels in `wheelhouse-lock.json`.
Download each listed URL locally and verify its SHA-256 before transfer to the
builder. Build with the actual digest-pinned base from the preceding step. The
build verifies every wheel again and installs dependencies without network
access. No registry credentials belong in the build directory or remote Docker
configuration. Use local archive/Skopeo publication and record the original
source revision, recipe hashes, build identity, archive and registry readback.

`check-policy.json` is the exact allowed command union for this image. The
archive-receipts Product qualification selects its Python command (six tests),
and queue-worker selects its Node TAP command (three tests). Both use the same
bundle, dependency-lock and oracle identities. Do not run both commands against
either repository. The source test hashes in `fixture-bundle.json` must match
the separately selected immutable fixture revisions.

The Python fixture uses a real PyPI packaging 24.2-to-25.0 upgrade with uv 0.13.5
locks. The test plugin selects the matching preinstalled version from the
immutable check image before collection; it installs nothing at runtime.
`test_dependency_runtime.py` checks the imported package version against the
source requirement and exercises version parsing. The provider may modify only
the admitted manifest/lock paths, so these independent tests remain fixed.

After both base digests exist, the protected `ADP_L5_TRANSFORM_IMAGE_CONFIG_JSON`
contains `schema_version: 1`, the actual `provider_base_image`, the actual
`check_base_image`, and this `check_policy`. The final provider/check images are
built by the real dispatched main release workflow using its actual run/attempt
and immutable wheel/archive receipt. Local operator base-build journals must
identify themselves as `operator-cli`; never manufacture GitHub run identity.

Only Transform's publication job selects the isolated `ubuntu-24.04` hosted
runner, which supplies Docker. Installation and live qualification retain ARC
for private connectivity. The existing protected environment and OIDC deployment
role remain required. A failed hosted assignment produces an ARC diagnostic for
organization runner policy, Actions usage limits and billing; repair the reported
cause and rerun the original publication job. No automatic remote-daemon fallback
is configured.

The hosted job retains its publication context, artifact coordinates and image
receipts using the existing protected release role and versioned
`l5/security/<source>/<wheel>/publication/<repository>/<run>/<attempt>/` prefix.
Each conditional write is read back by its exact version before reporting the
receipt coordinates in job outputs. Image-build failures retain the completed
artifact receipts with an explicit unsuccessful outcome. Installation and
qualification continue selecting the original immutable artifact manifest.
Ordinary CI receipt upload remains restricted to ARC's IRSA identity.
