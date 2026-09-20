"""An unpinned or drifted dependency set is refused — Issue #5530 (w6-07).

Covers AC-02's **unpinned dependencies** case, and the reproducible-offline-rendering half
of Design item 1.

## The two legacy mechanisms these tests close

`03-deploy-rgds.sh` fetched the graphs it applied by cloning a third party's moving default
branch at deploy time, and reused `/tmp/kro` if it already existed — so on a reused runner it
applied whatever a previous run had left on disk. `test_a_branch_name_instead_of_a_commit_is_refused`
and the vendored-integrity tests are that defect, inverted.

`02-enable-eks-capabilities.sh` resolved each chart's "latest" from GitHub at deploy time and,
on failure, skipped that controller with a WARNING and continued — a transient network error
producing a partially-installed control plane reported as success.
`test_a_chart_without_a_digest_is_refused` and `test_refusal_is_total_not_partial` are that
defect, inverted: there is no path that returns some of the dependency set.

## What these tests deliberately do NOT establish

That any chart was installed, that it reconciles, or that the recorded digests are what the
registries serve TODAY. These tests verify the lock is internally consistent and that the
vendored files on disk match their recorded checksums — provenance and integrity, not
liveness. Re-resolving a digest against a registry requires network access and is not part
of this offline suite; `dependencies.lock.yaml` records the exact method per source so the
values can be re-derived deliberately.
"""

from __future__ import annotations

import hashlib
import shutil

import pytest
import yaml
from account_factory import dependencies
from account_factory.dependencies import DependencyError, default_lock_path, load

EXPECTED_CHARTS = {"kro", "ack-organizations", "ack-ec2", "ack-eks", "ack-iam"}
# Vendored third-party graphs: checksum-verified against the lock.
EXPECTED_VENDORED_GRAPHS = {
    "01-network-stack.yaml": "NetworkStack",
    "02-eks-cluster-stack.yaml": "EKSClusterStack",
    "03-full-account-infrastructure.yaml": "FullAccountInfrastructure",
}

# ADP's own graphs under `manifests/`: required to exist and parse, deliberately NOT
# checksum-verified. A vendored file's checksum answers "is this still the third-party content
# we reviewed"; a file ADP authors has no such question, because changing it IS the review. The
# two are listed separately here so a maintained graph cannot quietly be treated as vendored
# provenance, or the reverse.
EXPECTED_MAINTAINED_GRAPHS = {
    "adp-account-ownership.yaml": "AccountOwnership",
    "adp-workspace-infrastructure.yaml": "WorkspaceInfrastructure",
}

EXPECTED_GRAPHS = {**EXPECTED_VENDORED_GRAPHS, **EXPECTED_MAINTAINED_GRAPHS}


@pytest.fixture
def lock_dir(tmp_path, module_dir):
    """A writable copy of the module's lock + vendor tree, for mutation tests.

    Copied rather than edited in place: a test that corrupts the real vendored files to
    prove corruption is detected would leave the repository in the corrupted state if it
    failed partway.
    """
    shutil.copy(
        module_dir / "dependencies.lock.yaml", tmp_path / "dependencies.lock.yaml"
    )
    shutil.copytree(module_dir / "vendor", tmp_path / "vendor")
    # The maintained graphs too: `load` requires them to be present, so a copy without them
    # would fail for that reason instead of the one each mutation test is probing.
    shutil.copytree(module_dir / "manifests", tmp_path / "manifests")
    return tmp_path


def _write_lock(lock_dir, mutate) -> object:
    """Apply `mutate` to the parsed lock, write it back, and return the path."""
    path = lock_dir / "dependencies.lock.yaml"
    data = yaml.safe_load(path.read_text())
    mutate(data)
    path.write_text(yaml.safe_dump(data))
    return path


# ── The real lock is verifiable as committed ──────────────────────────────────────────


def test_the_committed_lock_loads():
    """The positive control: every negative test below mutates a copy of THIS lock."""
    deps = load()
    assert {chart.name for chart in deps.charts} == EXPECTED_CHARTS
    assert {graph.filename for graph in deps.resource_graphs} == set(EXPECTED_GRAPHS)


def test_every_chart_is_pinned_by_content_digest():
    for chart in load().charts:
        assert chart.digest.startswith("sha256:")
        assert len(chart.digest) == len("sha256:") + 64


def test_the_install_reference_is_digest_addressed_not_tag_addressed():
    """A tag is a label someone can move; the reference an install uses must be content."""
    for chart in load().charts:
        reference = chart.oci_reference
        assert reference.startswith("oci://")
        assert f"@{chart.digest}" in reference
        # The version appears in the lock for auditability, but must not be what is fetched.
        assert not reference.endswith(f":{chart.version}")


def test_the_upstream_revision_is_a_commit_not_a_branch():
    revision = load().upstream_revision
    assert len(revision) == 40
    assert all(character in "0123456789abcdef" for character in revision)


def test_the_vendored_licence_is_present_and_recorded():
    """A recorded licence name without the licence text is not provenance."""
    deps = load()
    assert deps.license_name == "Apache-2.0"
    licence = default_lock_path().parent / "vendor" / "kro-account-factory" / "LICENSE"
    assert licence.is_file()
    assert "Apache License" in licence.read_text()


def test_each_graph_declares_the_kind_the_lock_claims():
    """The lock's `declares` is what `render.py` checks rendered kinds against.

    If it were merely a comment, a rendered custom resource could have no definition behind
    it while the lock appeared to cover it.
    """
    for graph in load().resource_graphs:
        documents = graph.read_documents()
        kinds = set()
        for document in documents:
            assert document.get("kind") == "ResourceGraphDefinition", graph.filename
            kinds.add((document.get("spec") or {}).get("schema", {}).get("kind"))
        assert graph.declares in kinds, (
            f"{graph.filename} does not declare {graph.declares}"
        )
        assert EXPECTED_GRAPHS[graph.filename] == graph.declares


def test_vendored_and_maintained_graphs_are_distinguishable():
    """Provenance travels with each graph, because the two carry different obligations.

    A vendored file must never be edited in place and has an upstream revision to re-vendor
    from; a maintained file has neither. Collapsing them would lose the distinction that makes
    the checksum asymmetry defensible.
    """
    graphs = {graph.filename: graph for graph in load().resource_graphs}
    for filename in EXPECTED_VENDORED_GRAPHS:
        assert graphs[filename].provenance == "vendored"
        assert graphs[filename].sha256, "a vendored graph is pinned by checksum"
    for filename in EXPECTED_MAINTAINED_GRAPHS:
        assert graphs[filename].provenance == "adp-maintained"


def test_a_missing_maintained_graph_is_refused(lock_dir):
    """Rendering an object whose graph is absent would create a resource nothing reconciles.

    Maintained graphs are not checksum-verified, so this is the check that still makes them
    required — without it, "no checksum" would shade into "not really needed".
    """
    (lock_dir / "manifests" / "adp-workspace-infrastructure.yaml").unlink()
    with pytest.raises(DependencyError, match="missing"):
        load(lock_dir / "dependencies.lock.yaml")


def test_a_maintained_graph_that_declares_something_else_is_refused(lock_dir):
    """The file and the lock must agree about what applying it creates."""
    path = lock_dir / "manifests" / "adp-workspace-infrastructure.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "kind: WorkspaceInfrastructure", "kind: SomethingElse", 1
        ),
        encoding="utf-8",
    )
    with pytest.raises(DependencyError, match="but the lock records"):
        load(lock_dir / "dependencies.lock.yaml")


def test_every_graph_exposes_the_schema_rendering_checks_against():
    """`render.py` compares each rendered object against its graph's declared inputs.

    A graph with no parsed schema would silently skip that comparison, which is how AF-001's
    missing required inputs went unnoticed.
    """
    for graph in load().resource_graphs:
        assert graph.schema is not None, graph.filename
        assert graph.schema.kind == graph.declares
        assert graph.schema.required_inputs or graph.schema.optional_inputs


def test_the_required_optional_split_follows_the_graphs_own_default_marker():
    """Required vs optional is read from kro's `default=` marker, not maintained separately.

    `subnetIds: '[]string'` is required; `nodeGroupMinSize: integer | default=0` is not. That
    distinction is what makes a missing REQUIRED input checkable.
    """
    schema = load().schema_for("EKSClusterStack")
    assert "subnetIds" in schema.required_inputs
    assert "securityGroupIds" in schema.required_inputs
    assert schema.optional_inputs
    assert not schema.required_inputs & schema.optional_inputs


def test_an_undeclared_kind_has_no_schema_and_says_so():
    """Raising beats returning None: a kind with no graph must fail, not skip its check."""
    with pytest.raises(DependencyError, match="no resource graph declares"):
        load().schema_for("NotAKindAnyGraphDeclares")


def test_chart_lookup_by_name_refuses_an_unknown_chart():
    with pytest.raises(DependencyError, match="no chart named"):
        load().chart("ack-nonexistent")


# ── AC-02: unpinned dependencies are refused ──────────────────────────────────────────


def test_a_chart_without_a_digest_is_refused(lock_dir):
    """The `latest`-at-deploy-time defect: a version-only pin is not a pin."""

    def drop_digest(data):
        del data["charts"]["kro"]["digest"]

    path = _write_lock(lock_dir, drop_digest)
    with pytest.raises(DependencyError) as raised:
        load(path)
    assert "no valid sha256 content digest" in str(raised.value)


@pytest.mark.parametrize(
    "digest",
    [
        "latest",
        "0.9.4",
        "sha256:notahexdigest",
        "sha256:4e8de4d3",  # truncated
        "md5:4e8de4d34b7e2c3cede958e140e26cd6",
        "",
    ],
)
def test_a_digest_that_is_not_a_full_sha256_is_refused(lock_dir, digest):
    path = _write_lock(
        lock_dir, lambda data: data["charts"]["ack-iam"].__setitem__("digest", digest)
    )
    with pytest.raises(DependencyError, match="content digest"):
        load(path)


@pytest.mark.parametrize("field", ["registry", "repository", "version", "namespace"])
def test_a_chart_missing_a_required_field_is_refused(lock_dir, field):
    path = _write_lock(lock_dir, lambda data: data["charts"]["ack-eks"].pop(field))
    with pytest.raises(DependencyError, match=f"missing `{field}`"):
        load(path)


def test_a_branch_name_instead_of_a_commit_is_refused(lock_dir):
    """`git clone --depth 1 <default branch>` applies whatever the branch holds right now."""
    path = _write_lock(
        lock_dir,
        lambda data: data["resource_graph_definitions"]["upstream"].__setitem__(
            "revision", "main"
        ),
    )
    with pytest.raises(DependencyError) as raised:
        load(path)
    assert "not a full commit sha" in str(raised.value)


def test_a_short_revision_is_refused(lock_dir):
    path = _write_lock(
        lock_dir,
        lambda data: data["resource_graph_definitions"]["upstream"].__setitem__(
            "revision", "3b6e8c5"
        ),
    )
    with pytest.raises(DependencyError, match="not a full commit sha"):
        load(path)


def test_a_missing_licence_record_is_refused(lock_dir):
    path = _write_lock(
        lock_dir,
        lambda data: data["resource_graph_definitions"]["upstream"].pop("license"),
    )
    with pytest.raises(DependencyError, match="license is absent"):
        load(path)


def test_a_recorded_licence_whose_file_is_absent_is_refused(lock_dir):
    (lock_dir / "vendor" / "kro-account-factory" / "LICENSE").unlink()
    with pytest.raises(DependencyError, match="not present"):
        load(lock_dir / "dependencies.lock.yaml")


def test_an_empty_chart_set_does_not_pass_vacuously(lock_dir):
    """Every pinning check passing because there is nothing to check is not a pass."""
    path = _write_lock(lock_dir, lambda data: data.__setitem__("charts", {}))
    with pytest.raises(DependencyError, match="pins no charts"):
        load(path)


def test_an_empty_graph_set_does_not_pass_vacuously(lock_dir):
    path = _write_lock(
        lock_dir,
        lambda data: data["resource_graph_definitions"].__setitem__("files", {}),
    )
    with pytest.raises(DependencyError, match="records no resource graph definitions"):
        load(path)


# ── Vendored-file integrity: the lock's checksums are enforced, not decorative ────────


def test_a_vendored_file_edited_without_updating_the_lock_is_refused(lock_dir):
    """Without this check, `vendor/` is just files someone could edit."""
    graph = lock_dir / "vendor" / "kro-account-factory" / "01-network-stack.yaml"
    graph.write_text(graph.read_text() + "\n# an unreviewed local edit\n")
    with pytest.raises(DependencyError) as raised:
        load(lock_dir / "dependencies.lock.yaml")
    message = str(raised.value)
    assert "does not match the dependency lock" in message
    # Both hashes are shown, so the operator can tell drift from a stale lock.
    assert "lock:" in message and "actual:" in message


def test_a_lock_checksum_edited_without_updating_the_file_is_refused(lock_dir):
    """Drift is detected from either side."""
    path = _write_lock(
        lock_dir,
        lambda data: data["resource_graph_definitions"]["files"][
            "02-eks-cluster-stack.yaml"
        ].__setitem__("sha256", "0" * 64),
    )
    with pytest.raises(DependencyError, match="does not match the dependency lock"):
        load(path)


def test_a_missing_vendored_file_is_refused_and_not_fetched(lock_dir):
    """Rendering must not fall back to fetching it — that fallback IS the legacy defect."""
    (
        lock_dir
        / "vendor"
        / "kro-account-factory"
        / "03-full-account-infrastructure.yaml"
    ).unlink()
    with pytest.raises(DependencyError) as raised:
        load(lock_dir / "dependencies.lock.yaml")
    assert "is missing" in str(raised.value)


def test_the_recorded_checksums_match_the_committed_files():
    """Recompute independently of `load`, so this test fails if `load`'s check regresses."""
    lock = yaml.safe_load(default_lock_path().read_text())
    vendor = default_lock_path().parent / "vendor" / "kro-account-factory"
    for filename, entry in lock["resource_graph_definitions"]["files"].items():
        actual = hashlib.sha256((vendor / filename).read_bytes()).hexdigest()
        assert actual == entry["sha256"], filename


@pytest.mark.parametrize("sha", ["", "0" * 63, "z" * 64, "SHA256:" + "0" * 64])
def test_a_malformed_recorded_checksum_is_refused(lock_dir, sha):
    path = _write_lock(
        lock_dir,
        lambda data: data["resource_graph_definitions"]["files"][
            "01-network-stack.yaml"
        ].__setitem__("sha256", sha),
    )
    with pytest.raises(DependencyError, match="not a sha256 hex digest"):
        load(path)


def test_a_graph_that_does_not_record_what_it_declares_is_refused(lock_dir):
    path = _write_lock(
        lock_dir,
        lambda data: data["resource_graph_definitions"]["files"][
            "01-network-stack.yaml"
        ].pop("declares"),
    )
    with pytest.raises(DependencyError, match="does not record what it declares"):
        load(path)


# ── Refusal is total ─────────────────────────────────────────────────────────────────


def test_refusal_is_total_not_partial(lock_dir):
    """No "skip this one and continue" path exists, because that path is the defect.

    The legacy script's `continue`-on-failure is precisely what turned a network error into
    a partially-installed control plane reported as success. `load` either returns a fully
    verified set or raises.
    """

    def break_one_chart(data):
        data["charts"]["ack-ec2"]["digest"] = "latest"

    path = _write_lock(lock_dir, break_one_chart)
    with pytest.raises(DependencyError):
        load(path)
    # And there is no alternative entry point that would return the other four charts.
    assert not [
        name
        for name in dir(dependencies)
        if name.startswith(("load_partial", "try_load", "load_best_effort"))
    ]


def test_an_unreadable_lock_is_refused(tmp_path):
    with pytest.raises(DependencyError, match="could not read"):
        load(tmp_path / "does-not-exist.yaml")


def test_an_unparsable_lock_is_refused(tmp_path):
    path = tmp_path / "dependencies.lock.yaml"
    path.write_text("charts: [this is: not valid yaml\n")
    with pytest.raises(DependencyError, match="not valid YAML"):
        load(path)


def test_a_lock_that_is_not_a_mapping_is_refused(tmp_path):
    path = tmp_path / "dependencies.lock.yaml"
    path.write_text("- just\n- a list\n")
    with pytest.raises(DependencyError, match="not a mapping"):
        load(path)


# ── The lock states what it has not resolved ─────────────────────────────────────────


def test_the_lock_records_the_live_target_as_unresolved():
    """A placeholder target would look like an answer. It must say it has none.

    Live target selection and spend limits are the EPIC A supervisor's to settle; this
    module's offline work does not and cannot decide them.
    """
    lock = yaml.safe_load(default_lock_path().read_text())
    assert lock["target"]["status"] == "unresolved"
    assert lock["target"]["supplied_per_request"]
