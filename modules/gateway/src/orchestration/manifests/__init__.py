"""Reviewed deployment manifests, packaged as data inside the runtime image.

A real package with an `__init__.py` rather than a bare directory, so that
`importlib.resources.files()` has an importable anchor. Reaching the file by
`Path(__file__).parents[n]` instead would resolve relative to the source tree and
break once the package is installed rather than run from a checkout — exactly the
difference between a test environment and the image.

The manifest lives here — inside the gateway's own `src/` tree — rather than at
the repository root, because the gateway image is built with `modules/gateway` as
its docker context (`codebuild/bs-gateway-build.yml`). A repo-root `config/`
directory is simply not in that context: no `COPY` could reach it, so a manifest
kept there is unreachable from the deployed process by construction.

Nothing here is executable, and nothing in this package is imported for its
behaviour: `deployment_manifest.load_packaged_manifest()` reads the YAML text and
parses it through the same reviewed validation every other caller uses.
"""
