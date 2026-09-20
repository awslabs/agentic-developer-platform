"""Dependency pinning and vendored-file integrity — Issue #5530 (w6-07), EPIC #4910.

## The two ways the legacy flow could not say what it deployed

Both are quoted in `dependencies.lock.yaml`'s header; briefly, `03-deploy-rgds.sh` cloned a
third party's moving default branch at deploy time (and REUSED `/tmp/kro` if it already
existed, so on a reused runner it applied whatever a previous run had left behind), and
`02-enable-eks-capabilities.sh` asked GitHub for each controller chart's "latest" version,
then on failure skipped that controller with a WARNING and continued — turning a transient
network error into a partially-installed control plane reported as success.

## What this module enforces instead

`load` reads the lock and refuses it unless every dependency is pinned by content digest,
and unless every vendored file on disk still hashes to what the lock claims. That second
check is what makes the vendored copy trustworthy: without it, `vendor/` is just files
someone could edit, and the lock's checksums would be decoration.

Refusal is total. There is no partial success and no "skip this one and continue" path,
because that path is the defect. Rendering calls `load` before producing anything.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

__all__ = [
    "ChartPin",
    "DependencyError",
    "GraphSchema",
    "PinnedDependencies",
    "ResourceGraphPin",
    "default_lock_path",
    "load",
]

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class DependencyError(Exception):
    """The dependency set is not verifiably pinned, so nothing may be rendered from it."""


@dataclass(frozen=True)
class ChartPin:
    """One controller chart, pinned by digest.

    `version` is retained for auditability only; `digest` is the pin. Same split as
    ../../../releases/superplane.lock.yaml uses for images, and for the same reason: a
    version string is a label someone can move.
    """

    name: str
    registry: str
    repository: str
    version: str
    digest: str
    namespace: str

    @property
    def oci_reference(self) -> str:
        """The digest-addressed reference an install operation uses.

        Digest-addressed, not tag-addressed, so the reference cannot resolve to different
        content later. `helm` accepts this form for OCI charts.
        """
        return f"oci://{self.registry}/{self.repository}@{self.digest}"


@dataclass(frozen=True)
class GraphSchema:
    """What one resource graph requires of a custom resource that instantiates it.

    Read from the graph's own `spec.schema.spec`, so this is the graph's statement about
    itself rather than a second list someone maintains alongside it and forgets to update.

    The required/optional split is kro's `default=` marker: `subnetIds: '[]string'` is
    required, `nodeGroupMinSize: integer | default=0` is not. That distinction is what makes
    this checkable — a rendered object missing a REQUIRED input cannot reconcile, which is
    the defect class this type exists to catch (see `render.py`'s `_check_graph_inputs`).
    """

    kind: str
    required_inputs: frozenset[str]
    optional_inputs: frozenset[str]

    @property
    def known_inputs(self) -> frozenset[str]:
        return self.required_inputs | self.optional_inputs


def _parse_schema(document: dict, source: str) -> GraphSchema:
    """Extract a graph's declared inputs from its ResourceGraphDefinition document."""
    spec = document.get("spec")
    if not isinstance(spec, dict):
        raise DependencyError(f"{source}: ResourceGraphDefinition has no `spec`")
    schema = spec.get("schema")
    if not isinstance(schema, dict):
        raise DependencyError(f"{source}: ResourceGraphDefinition has no `spec.schema`")
    kind = schema.get("kind")
    if not isinstance(kind, str) or not kind:
        raise DependencyError(f"{source}: `spec.schema.kind` is absent")
    fields = schema.get("spec")
    if not isinstance(fields, dict) or not fields:
        raise DependencyError(
            f"{source}: `spec.schema.spec` declares no inputs, so a rendered object could "
            f"not be checked against it"
        )

    required: set[str] = set()
    optional: set[str] = set()
    for name, declaration in fields.items():
        # A non-string declaration (a nested mapping) is treated as required: it cannot
        # carry kro's `default=` marker, and guessing "optional" would silently excuse a
        # missing input.
        text = declaration if isinstance(declaration, str) else ""
        (optional if "default=" in text else required).add(str(name))
    return GraphSchema(
        kind=kind,
        required_inputs=frozenset(required),
        optional_inputs=frozenset(optional),
    )


@dataclass(frozen=True)
class ResourceGraphPin:
    """One resource graph definition available to rendering.

    Covers both the vendored upstream graphs (checksum-verified against the lock) and ADP's
    own maintained graphs under `manifests/`. `provenance` distinguishes them, because
    "third-party code we pinned" and "code we wrote" carry different review obligations and
    conflating them would lose that: a maintained file has no upstream revision to re-vendor
    from, and a vendored file must never be edited in place.
    """

    filename: str
    sha256: str
    declares: str
    path: Path
    provenance: str = "vendored"
    schema: GraphSchema | None = None

    def read_documents(self) -> list[dict]:
        """Parse the file. For a vendored file, only after `load` verified its checksum."""
        text = self.path.read_text(encoding="utf-8")
        documents = [doc for doc in yaml.safe_load_all(text) if isinstance(doc, dict)]
        if not documents:
            raise DependencyError(
                f"{self.filename} contains no Kubernetes objects, so applying it would "
                f"apply nothing while appearing to succeed"
            )
        return documents


@dataclass(frozen=True)
class PinnedDependencies:
    """The verified dependency set. Existence of this object means the lock checked out."""

    charts: tuple[ChartPin, ...]
    resource_graphs: tuple[ResourceGraphPin, ...]
    upstream_revision: str
    upstream_repository: str
    license_name: str

    def chart(self, name: str) -> ChartPin:
        for pin in self.charts:
            if pin.name == name:
                return pin
        raise DependencyError(f"no chart named {name!r} in the dependency lock")

    def schema_for(self, kind: str) -> GraphSchema:
        """The declared input schema for a custom-resource kind.

        Raises rather than returning None: a rendered kind with no graph behind it is a
        custom resource nothing would reconcile, which must fail rather than skip its check.
        """
        for graph in self.resource_graphs:
            if graph.schema is not None and graph.schema.kind == kind:
                return graph.schema
        raise DependencyError(
            f"no resource graph declares {kind!r}, so a rendered object of that kind could "
            f"not be checked against a schema"
        )


def default_lock_path() -> Path:
    """The lock beside this package."""
    return Path(__file__).resolve().parent.parent / "dependencies.lock.yaml"


def _require_mapping(value: object, what: str) -> dict:
    if not isinstance(value, dict):
        raise DependencyError(f"{what} is not a mapping in the dependency lock")
    return value


def _load_maintained(lock: dict, root: Path) -> list[ResourceGraphPin]:
    """Load ADP's own resource graphs from `manifests/`.

    Deliberately NOT checksum-verified, and that asymmetry is the point. A vendored file's
    checksum answers "is this still the third-party content we reviewed"; a maintained file
    has no such question, because changing it IS the review — it arrives through a diff on
    this repository like any other source. Recording a checksum for a file we author would
    turn every intentional edit into a two-place update whose only failure mode is forgetting
    the second place.

    They are still REQUIRED to be present and parsable: a missing maintained graph must fail
    here, because rendering an object whose graph is absent produces a custom resource
    nothing reconciles.
    """
    entry = lock.get("maintained_resource_graph_definitions")
    if entry is None:
        return []
    entry = _require_mapping(entry, "`maintained_resource_graph_definitions`")
    files_raw = _require_mapping(entry.get("files"), "maintained ... .files")
    if not files_raw:
        raise DependencyError(
            "`maintained_resource_graph_definitions.files` is empty. Declare the maintained "
            "graphs or remove the section; an empty set checks nothing"
        )
    directory = entry.get("directory")
    if not isinstance(directory, str) or not directory:
        raise DependencyError(
            "`maintained_resource_graph_definitions.directory` is absent"
        )

    manifests_dir = root / directory
    graphs: list[ResourceGraphPin] = []
    for filename, file_entry in sorted(files_raw.items()):
        file_entry = _require_mapping(file_entry, f"maintained ... .files.{filename}")
        declares = file_entry.get("declares")
        if not isinstance(declares, str) or not declares:
            raise DependencyError(
                f"maintained_resource_graph_definitions.files.{filename} does not record "
                f"what it declares"
            )
        path = manifests_dir / filename
        if not path.is_file():
            raise DependencyError(
                f"the maintained resource graph {filename} is missing from {manifests_dir}. "
                f"Rendering an object it declares would create a custom resource with no "
                f"definition behind it"
            )
        pin = ResourceGraphPin(
            filename=filename,
            # A maintained file's identity is its reviewed content in this repository, not a
            # recorded hash. Stated rather than left as an empty-string mystery.
            sha256="",
            declares=declares,
            path=path,
            provenance="adp-maintained",
        )
        schema = _parse_schema(pin.read_documents()[0], filename)
        if schema.kind != declares:
            raise DependencyError(
                f"{filename} declares {schema.kind!r} but the lock records {declares!r}"
            )
        graphs.append(
            ResourceGraphPin(
                filename=filename,
                sha256="",
                declares=declares,
                path=path,
                provenance="adp-maintained",
                schema=schema,
            )
        )
    return graphs


def load(
    lock_path: Path | None = None, *, vendor_root: Path | None = None
) -> PinnedDependencies:
    """Read and verify the dependency lock.

    Raises `DependencyError` on any of: unreadable/unparsable lock, a chart without a
    content digest, a chart pinned only by a mutable tag, a vendored file that is missing,
    or a vendored file whose contents no longer match the lock's checksum.

    Every one of those is a refusal. A dependency set that cannot be verified is not a
    dependency set that may be deployed with a warning.
    """
    lock_path = lock_path or default_lock_path()
    root = vendor_root or lock_path.parent

    try:
        lock = yaml.safe_load(lock_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise DependencyError(
            f"could not read the dependency lock {lock_path}: {exc}"
        ) from exc
    except yaml.YAMLError as exc:
        raise DependencyError(f"{lock_path} is not valid YAML: {exc}") from exc

    lock = _require_mapping(lock, str(lock_path))

    charts_raw = _require_mapping(lock.get("charts"), "`charts`")
    if not charts_raw:
        raise DependencyError(
            f"{lock_path} pins no charts. An empty chart set would make every pinning check "
            f"pass vacuously"
        )

    charts: list[ChartPin] = []
    for name, entry in sorted(charts_raw.items()):
        entry = _require_mapping(entry, f"charts.{name}")
        digest = entry.get("digest")
        if not isinstance(digest, str) or not _DIGEST_RE.match(digest):
            raise DependencyError(
                f"charts.{name} has no valid sha256 content digest (got {digest!r}). A "
                f"chart pinned only by version is not pinned: the legacy flow resolved "
                f"'latest' at deploy time and could not say what it installed"
            )
        for required in ("registry", "repository", "version", "namespace"):
            if not isinstance(entry.get(required), str) or not entry[required]:
                raise DependencyError(f"charts.{name} is missing `{required}`")
        charts.append(
            ChartPin(
                name=name,
                registry=entry["registry"],
                repository=entry["repository"],
                version=str(entry["version"]),
                digest=digest,
                namespace=entry["namespace"],
            )
        )

    graphs_raw = _require_mapping(
        lock.get("resource_graph_definitions"), "`resource_graph_definitions`"
    )
    upstream = _require_mapping(
        graphs_raw.get("upstream"), "resource_graph_definitions.upstream"
    )
    revision = upstream.get("revision")
    if not isinstance(revision, str) or not re.match(r"^[0-9a-f]{40}$", revision):
        raise DependencyError(
            f"resource_graph_definitions.upstream.revision is {revision!r}, not a full "
            f"commit sha. A branch name here is the legacy defect: `git clone --depth 1` of "
            f"a default branch applies whatever that branch held at clone time"
        )
    license_name = upstream.get("license")
    if not isinstance(license_name, str) or not license_name:
        raise DependencyError(
            "resource_graph_definitions.upstream.license is absent. Vendored third-party "
            "files must record their licence"
        )
    license_file = upstream.get("license_file")
    if not isinstance(license_file, str) or not (root / license_file).is_file():
        raise DependencyError(
            f"the vendored licence file {license_file!r} is not present. A recorded licence "
            f"name without the licence text is not provenance"
        )

    files_raw = _require_mapping(
        graphs_raw.get("files"), "resource_graph_definitions.files"
    )
    if not files_raw:
        raise DependencyError(
            f"{lock_path} records no resource graph definitions, so the integrity check "
            f"would pass over nothing"
        )

    vendor_dir = root / "vendor" / "kro-account-factory"
    graphs: list[ResourceGraphPin] = []
    for filename, entry in sorted(files_raw.items()):
        entry = _require_mapping(entry, f"resource_graph_definitions.files.{filename}")
        expected = entry.get("sha256")
        if not isinstance(expected, str) or not _SHA256_RE.match(expected):
            raise DependencyError(
                f"resource_graph_definitions.files.{filename}.sha256 is {expected!r}, not a "
                f"sha256 hex digest"
            )
        declares = entry.get("declares")
        if not isinstance(declares, str) or not declares:
            raise DependencyError(
                f"resource_graph_definitions.files.{filename} does not record what it "
                f"declares"
            )
        path = vendor_dir / filename
        if not path.is_file():
            raise DependencyError(
                f"the vendored resource graph {filename} is missing from {vendor_dir}. "
                f"Rendering must not fall back to fetching it — that is the legacy defect"
            )
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            raise DependencyError(
                f"{filename} does not match the dependency lock:\n"
                f"  lock:   {expected}\n"
                f"  actual: {actual}\n"
                f"A vendored file that has drifted from its recorded checksum has unknown "
                f"provenance. Re-vendor from the pinned revision, or update the lock in a "
                f"reviewed commit"
            )
        pin = ResourceGraphPin(
            filename=filename,
            sha256=expected,
            declares=declares,
            path=path,
            provenance="vendored",
        )
        documents = pin.read_documents()
        schema = _parse_schema(documents[0], filename)
        if schema.kind != declares:
            raise DependencyError(
                f"{filename} declares {schema.kind!r} but the lock records {declares!r}. "
                f"The lock and the file must agree about what applying it creates"
            )
        graphs.append(
            ResourceGraphPin(
                filename=filename,
                sha256=expected,
                declares=declares,
                path=path,
                provenance="vendored",
                schema=schema,
            )
        )

    graphs.extend(_load_maintained(lock, root))

    return PinnedDependencies(
        charts=tuple(charts),
        resource_graphs=tuple(graphs),
        upstream_revision=revision,
        upstream_repository=str(upstream.get("repository", "")),
        license_name=license_name,
    )
