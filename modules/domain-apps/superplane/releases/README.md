# Superplane release inputs and build lanes

The lock records one verified SkyPilot image digest and three pending Superplane
images. Pending images are not deployable. Buildspecs and `build-image.sh` are
owned by this domain app; they build/push only the selected `adp-superplane-*`
repository and never invoke Terraform, Kubernetes rollout or platform deployment.
ECR repositories and the existing build role must already be provisioned through
separately approved infrastructure work. These lanes do not create platform roles.

The source-access grant is still unresolved. All three workflows stop before AWS
operations. Granting access alone is not enough: their explicit acquisition step
must be implemented for the approved mechanism. It must verify the pinned upstream
revision, stage its tree in `releases/source/` (including the `src/` component
paths), and write that verified full SHA to `source/.superplane-revision`. The
shared archive action includes this domain path in a per-run source archive.
The marker is checked by the build script but is not a substitute for acquisition
verification. Never use the read-only reference snapshot as source.

The buildspecs are tested offline with stub AWS/Docker tools; no real image has
been built here. Source identity and digest references are pinned, but upstream
Docker base tags and dependency downloads are not hermetic. An unchanged source
lock alone does not promise byte-identical newly built images; releases consume
the produced registry digest, after a reviewed update to the lock.

When a first build is verified, promote its entry in a reviewed lock update:
move its source path, ECR repository and workflow metadata from `pending_images`
to `image_sources`, remove `blocked_by`, add registry provenance, and record the
produced digest under `images`. The resolver retains those source inputs for
subsequent builds. An image cannot be both pending and resolved; removing its
source metadata makes rebuilding fail closed.
