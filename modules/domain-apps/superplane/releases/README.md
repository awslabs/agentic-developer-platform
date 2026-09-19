# Superplane release inputs and build lanes

The lock records one verified SkyPilot image digest and three pending Superplane
images. Pending images are not deployable. Buildspecs and `build-image.sh` are
owned by this domain app; they build/push only the selected `adp-superplane-*`
repository and never invoke Terraform, Kubernetes rollout or platform deployment.
ECR repositories and the existing build role must already be provisioned through
separately approved infrastructure work. These lanes do not create platform roles.

## Where the source comes from

The source-access grant is **resolved** — by removing the need for it. Issue #5326
(U22) transferred the three components into this repository under
`modules/domain-apps/superplane/src/`, so a build context is a directory in the
checkout: `actions/checkout` of *this* repository is the whole of source
acquisition, and no upstream read access, PAT or reference tree is involved. Each
lane's `Verify maintained source is present` step confirms the directory the lock
names is really here, so a lock entry renamed without moving files fails with a
message about the lock rather than a missing-file error inside `docker build`.

Two facts are recorded separately in the lock, and must stay separate:
`maintained_source` / `source_path` is **where the code lives now** (what a build
reads), while `upstream` is the **historical origin** of the transferred files
(provenance for the image label, never a fetch target). Collapsing them into one
"revision" would remove the ability to tell whether the maintained files have
changed since the transfer. `src/TRANSFER-MANIFEST.md` records the path map.

The fail-closed path (`resolve_lock.py` exit 78) is retained, not deleted: it
remains correct for any component whose `source_access` is unresolved, and keeping
it means such a component stops with a named cause instead of a checkout error
that reads like a transient CI fault.

Never use the read-only reference snapshot as source. It is evidence at upstream
`5d543c95`; building from it would ship evidence as a product, and it must not
become a second writable runtime tree alongside the maintained one.

## Image tags

Images are tagged with the **ADP commit**, and the origin revision is preserved as
an image label. This inverted with the transfer: while the source lived upstream,
the upstream revision identified the artifact; now it is frozen at the transfer
point, so tagging by it would make every later fix overwrite the previous image
under an unchanged tag.

The buildspecs are tested offline with stub AWS/Docker tools; no real image has
been built here. Source identity and digest references are pinned, but Docker base
tags and dependency downloads are not hermetic — and for the API specifically, its
`pyproject.toml` carries an unbounded `fastapi>=0.115.0` floor that currently
resolves to a version where no router registers (see `transfer-constraints.txt`).
An unchanged source lock alone does not promise byte-identical newly built images;
releases consume the produced registry digest, after a reviewed update to the lock.

When a first build is verified, promote its entry in a reviewed lock update:
move its source path, ECR repository and workflow metadata from `pending_images`
to `image_sources`, remove `blocked_by`, add registry provenance, and record the
produced digest under `images`. The resolver retains those source inputs for
subsequent builds. An image cannot be both pending and resolved; removing its
source metadata makes rebuilding fail closed.
