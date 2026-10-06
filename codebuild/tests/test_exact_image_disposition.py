"""Synthetic OCI/report fixtures only; no vulnerability payloads or network."""

import base64
import copy
import importlib.util
import io
import subprocess
import tarfile
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "disposition", Path(__file__).parents[1] / "exact_image_disposition.py"
)
D = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(D)


def tar_bytes(entries):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name, content in entries:
            member = tarfile.TarInfo(name)
            member.size = len(content)
            member.mode = 0o644
            archive.addfile(member, io.BytesIO(content))
    return output.getvalue()


def layer_archive(tmp_path, layers):
    config = D.canonical(
        {
            "architecture": "amd64",
            "os": "linux",
            "rootfs": {"diff_ids": ["sha256:" + D.sha(layer) for layer in layers]},
        }
    )

    def descriptor(data, media):
        return {
            "digest": "sha256:" + D.sha(data),
            "size": len(data),
            "mediaType": media,
        }

    manifest = D.canonical(
        {
            "schemaVersion": 2,
            "config": descriptor(config, "application/vnd.oci.image.config.v1+json"),
            "layers": [
                descriptor(layer, "application/vnd.oci.image.layer.v1.tar")
                for layer in layers
            ],
        }
    )
    archive = tmp_path / "layers.oci.tar"
    archive.write_bytes(
        tar_bytes(
            [
                ("oci-layout", b'{"imageLayoutVersion":"1.0.0"}'),
                *[
                    ("blobs/sha256/" + D.sha(data), data)
                    for data in [config, manifest, *layers]
                ],
            ]
        )
    )
    return archive, "sha256:" + D.sha(manifest)


def test_opaque_whiteout_and_directory_replacement(tmp_path):
    layers = [
        tar_bytes([("tree/old", b"old"), ("other/child", b"child")]),
        tar_bytes(
            [
                ("tree/.wh..wh..opq", b""),
                ("tree/new", b"new"),
                ("other", b"replacement"),
            ]
        ),
    ]
    _, _, files, _ = D.collect_oci(*layer_archive(tmp_path, layers))
    assert "/tree/old" not in files and "/other/child" not in files
    assert files["/tree/new"]["sha256"] == D.sha(b"new")
    assert files["/other"]["sha256"] == D.sha(b"replacement")


def test_nonempty_whiteout_refused(tmp_path):
    layers = [
        tar_bytes([("tree/old", b"old")]),
        tar_bytes([("tree/.wh.old", b"nonempty")]),
    ]
    with pytest.raises(D.Invalid, match="whiteout representation"):
        D.collect_oci(*layer_archive(tmp_path, layers))


@pytest.mark.parametrize("name", ["tree/.wh.", "tree/.wh..", "tree/.wh..."])
def test_invalid_whiteout_target_refused(tmp_path, name):
    with pytest.raises(D.Invalid, match="whiteout target basename"):
        D.collect_oci(*layer_archive(tmp_path, [tar_bytes([(name, b"")])]))


@pytest.mark.parametrize("entry", ["parent/child", "parent/.wh.child"])
def test_file_parent_for_write_or_whiteout_refused(tmp_path, entry):
    layers = [tar_bytes([("parent", b"file")]), tar_bytes([(entry, b"")])]
    with pytest.raises(D.Invalid, match="non-directory parent"):
        D.collect_oci(*layer_archive(tmp_path, layers))


def test_resolve_file_rejects_regular_file_ancestor():
    with pytest.raises(D.Invalid, match="non-directory path ancestor"):
        D.resolve_file(
            {"/parent": {"kind": "file"}, "/parent/child": {"kind": "file"}},
            "/parent/child",
        )


@pytest.fixture
def bundle(tmp_path):
    return make_bundle(tmp_path)


def make_bundle(tmp_path, package_name="demo", version="1.0"):
    revision = "a" * 40
    content = b"harmless synthetic library bytes\n"
    status = f"Package: {package_name}\nStatus: install ok installed\nVersion: {version}\nArchitecture: amd64\n\n".encode()
    layer = tar_bytes(
        [
            ("usr/lib/demo.so", content),
            ("var/lib/dpkg/status", status),
            (f"var/lib/dpkg/info/{package_name}.list", b"/usr/lib/demo.so\n"),
            (
                "opt/awscli-adp/lib/aws-cli/libuuid.so.1",
                b"harmless embedded ELF fixture",
            ),
        ]
    )
    config = {
        "architecture": "amd64",
        "os": "linux",
        "rootfs": {"type": "layers", "diff_ids": ["sha256:" + D.sha(layer)]},
        "config": {"Labels": {"org.opencontainers.image.revision": revision}},
    }
    config_bytes = D.canonical(config)

    def descriptor(data, media):
        return {
            "digest": "sha256:" + D.sha(data),
            "size": len(data),
            "mediaType": media,
        }

    manifest = {
        "schemaVersion": 2,
        "config": descriptor(config_bytes, "application/vnd.oci.image.config.v1+json"),
        "layers": [descriptor(layer, "application/vnd.oci.image.layer.v1.tar")],
    }
    manifest_bytes = D.canonical(manifest)
    archive = tar_bytes(
        [
            ("oci-layout", b'{"imageLayoutVersion":"1.0.0"}'),
            (
                "index.json",
                D.canonical(
                    {
                        "schemaVersion": 2,
                        "manifests": [
                            descriptor(
                                manifest_bytes,
                                "application/vnd.oci.image.manifest.v1+json",
                            )
                        ],
                    }
                ),
            ),
            *[
                ("blobs/sha256/" + D.sha(data), data)
                for data in (layer, config_bytes, manifest_bytes)
            ],
        ]
    )
    image = "example.invalid/demo@sha256:" + D.sha(manifest_bytes)
    package = {
        "id": "package-id",
        "name": package_name,
        "version": version,
        "type": "deb",
        "purl": f"pkg:deb/debian/{package_name}@{version}?arch=amd64",
        "foundBy": "dpkg-db-cataloger",
        "metadataType": "dpkg-db-entry",
        "locations": [
            {
                "path": "/var/lib/dpkg/status",
                "accessPath": "/var/lib/dpkg/status",
                "layerID": "sha256:" + D.sha(layer),
                "annotations": {"evidence": "primary"},
            }
        ],
        "metadata": {"architecture": "amd64"},
    }
    source = {
        "type": "image",
        "metadata": {
            "imageID": "sha256:" + D.sha(config_bytes),
            "config": base64.b64encode(config_bytes).decode(),
        },
    }
    matches, rules, results = [], [], []
    for number in (1, 2):
        advisory = f"CVE-2000-000{number}"
        matches.append(
            {
                "artifact": package,
                "vulnerability": {
                    "id": advisory,
                    "namespace": "debian:test",
                    "severity": "High",
                },
            }
        )
        rule_id = advisory + "-" + package_name
        rules.append(
            {
                "id": rule_id,
                "help": {
                    "text": f"Vulnerability {advisory}\nData Namespace: debian:test\nPackage: {package_name}\nVersion: {version}\nType: deb\nSeverity: high"
                },
                "properties": {"purls": [package["purl"]], "security-severity": "7.5"},
            }
        )
        results.append(
            {
                "ruleId": rule_id,
                "message": {"text": "Synthetic test finding"},
                "locations": [
                    {
                        "physicalLocation": {
                            "artifactLocation": {"uri": "/var/lib/dpkg/status"}
                        }
                    }
                ],
            }
        )
    configuration = {"ignore": [], "only-fixed": False}
    native = {
        "descriptor": {
            "name": "grype",
            "version": "0.119.0",
            "configuration": configuration,
        },
        "source": source,
        "matches": matches,
        "ignoredMatches": [],
    }
    sarif = {
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {"name": "grype", "version": "0.119.0", "rules": rules}
                },
                "results": results,
            }
        ],
    }
    values = {
        "archive": archive,
        "sbom": D.canonical({"source": source, "artifacts": [package]}),
        "raw_json": D.canonical(native),
        "raw_sarif": D.canonical(sarif),
        "scanner_binary": b"synthetic scanner executable",
        "scanner_database": b"synthetic database",
        "scanner_config": D.canonical(configuration),
    }
    inputs = {}
    for key, data in values.items():
        inputs[key] = key + ".artifact"
        (tmp_path / inputs[key]).write_bytes(data)
    return tmp_path, inputs, image, revision


def observed(bundle):
    return D.collect(*bundle)


def receipt(bundle, observation, status="not_affected"):
    root = bundle[0]
    evidence = root / "synthetic-evidence.txt"
    evidence.write_text(
        "Synthetic evidence; no real review or release authorization.\n"
    )
    roles = (
        {"independent_review", "source", "patch", "regression", "build"}
        if status == "fixed"
        else {"independent_review", "applicability"}
    )
    occurrence = observation["occurrences"][0]
    files = observation["packages"][occurrence["package"]["purl"]]["files"]
    evidence_hashes = {evidence.name: D.file_sha(evidence)}
    if status == "fixed":
        staging = root / "package-staging"
        (staging / "DEBIAN").mkdir(parents=True)
        (staging / "usr/lib").mkdir(parents=True)
        (staging / "usr/lib/demo.so").write_bytes(b"harmless synthetic library bytes\n")
        (staging / "usr/lib/demo.so").chmod(0o644)
        (staging / "DEBIAN/control").write_text(
            "Package: demo\nVersion: 1.0\nArchitecture: amd64\nMaintainer: Synthetic <test@example.invalid>\nDescription: Harmless fixture\n"
        )
        subprocess.run(
            ["dpkg-deb", "--build", str(staging), str(root / "demo.deb")],
            check=True,
            capture_output=True,
        )
        evidence_hashes["demo.deb"] = D.file_sha(root / "demo.deb")
    return {
        "schema": "adp-exact-image-review/v1",
        "observation_sha256": D.object_sha(observation),
        "reviewer": "SYNTHETIC TEST REVIEWER",
        "evidence": evidence_hashes,
        "decisions": [
            {
                "native_sha256": occurrence["native_sha256"],
                "status": status,
                "rationale": "Synthetic decision only",
                "evidence": {role: evidence.name for role in roles},
                "files": copy.deepcopy(files),
                "package_artifact": "demo.deb" if status == "fixed" else None,
            }
        ],
    }


def derive(bundle, record, observation, pin=None):
    return D.derive(
        bundle[0],
        record,
        pin or D.object_sha(record),
        observation,
        (bundle[0] / bundle[1]["raw_sarif"]).read_bytes(),
    )


def change_json(bundle, key, mutate):
    path = bundle[0] / bundle[1][key]
    value = D.parse(path.read_bytes())
    mutate(value)
    path.write_bytes(D.canonical(value))


def test_collect_and_derive_keep_raw_and_active_finding(bundle):
    observation = observed(bundle)
    original = {key: (bundle[0] / name).read_bytes() for key, name in bundle[1].items()}
    record = receipt(bundle, observation)
    output, summary = derive(bundle, record, observation)
    assert (
        summary["raw"] == 2 and summary["not_affected"] == 1 and summary["active"] == 1
    )
    assert output["runs"][0]["results"][0]["suppressions"][0]["status"] == "accepted"
    assert "suppressions" not in output["runs"][0]["results"][1]
    for key, name in bundle[1].items():
        assert (bundle[0] / name).read_bytes() == original[key]
    # The only result change is its approved annotation.
    del output["runs"][0]["results"][0]["suppressions"]
    assert output == D.parse(original["raw_sarif"])


def test_fixed_count_is_separate(bundle):
    observation = observed(bundle)
    _, summary = derive(bundle, receipt(bundle, observation, "fixed"), observation)
    assert summary["fixed"] == 1 and summary["not_affected"] == 0


@pytest.mark.parametrize(
    "field", ["image", "config_digest", "source_revision", "files_sha256"]
)
def test_cross_image_scope_denied(bundle, field):
    observation = observed(bundle)
    record = receipt(bundle, observation)
    observation[field] += "-changed"
    with pytest.raises(D.Invalid, match="binding mismatch"):
        derive(bundle, record, observation)


@pytest.mark.parametrize(
    "key", ["scanner_binary", "scanner_database", "raw_sarif", "sbom"]
)
def test_changed_artifact_denied(bundle, key):
    observation = observed(bundle)
    record = receipt(bundle, observation)
    observation["inputs"][key] = "f" * 64
    with pytest.raises(D.Invalid, match="binding mismatch"):
        derive(bundle, record, observation)


def test_changed_decision_under_existing_pin_denied(bundle):
    observation = observed(bundle)
    record = receipt(bundle, observation)
    pin = D.object_sha(record)
    record["decisions"][0]["rationale"] = "changed"
    with pytest.raises(D.Invalid, match="approval pin"):
        derive(bundle, record, observation, pin)


def test_changed_evidence_denied(bundle):
    observation = observed(bundle)
    record = receipt(bundle, observation)
    (bundle[0] / "synthetic-evidence.txt").write_text("changed")
    with pytest.raises(D.Invalid, match="evidence"):
        derive(bundle, record, observation)


@pytest.mark.parametrize(
    "status", ["risk_accepted", "affected", "under_investigation", ""]
)
def test_unsupported_status_denied(bundle, status):
    observation = observed(bundle)
    record = receipt(bundle, observation)
    record["decisions"][0]["status"] = status
    with pytest.raises(D.Invalid, match="status"):
        derive(bundle, record, observation)


def test_duplicate_decision_denied(bundle):
    observation = observed(bundle)
    record = receipt(bundle, observation)
    record["decisions"].append(copy.deepcopy(record["decisions"][0]))
    with pytest.raises(D.Invalid, match="duplicate"):
        derive(bundle, record, observation)


def test_wrong_installed_file_denied(bundle):
    observation = observed(bundle)
    record = receipt(bundle, observation)
    record["decisions"][0]["files"]["/usr/lib/demo.so"]["sha256"] = "f" * 64
    with pytest.raises(D.Invalid, match="installed-file"):
        derive(bundle, record, observation)


def test_fixed_requires_patch_and_package_evidence(bundle):
    observation = observed(bundle)
    record = receipt(bundle, observation, "fixed")
    del record["decisions"][0]["evidence"]["patch"]
    with pytest.raises(D.Invalid, match="evidence roles"):
        derive(bundle, record, observation)


def test_nonpackage_artifact_cannot_claim_fixed(bundle):
    observation = observed(bundle)
    record = receipt(bundle, observation, "fixed")
    record["decisions"][0]["package_artifact"] = "synthetic-evidence.txt"
    with pytest.raises(D.Invalid, match="Debian package"):
        derive(bundle, record, observation)


def test_existing_sarif_suppression_denied(bundle):
    change_json(
        bundle,
        "raw_sarif",
        lambda d: d["runs"][0]["results"][0].update(
            suppressions=[{"status": "accepted"}]
        ),
    )
    with pytest.raises(D.Invalid, match="suppressions"):
        observed(bundle)


def test_ignored_native_finding_denied(bundle):
    change_json(
        bundle, "raw_json", lambda d: d["ignoredMatches"].append({"test": True})
    )
    with pytest.raises(D.Invalid, match="ignores"):
        observed(bundle)


def test_missing_sarif_result_denied(bundle):
    change_json(bundle, "raw_sarif", lambda d: d["runs"][0]["results"].pop())
    with pytest.raises(D.Invalid, match="omitted"):
        observed(bundle)


def test_ambiguous_native_result_denied(bundle):
    change_json(
        bundle,
        "raw_json",
        lambda d: d["matches"].append(copy.deepcopy(d["matches"][0])),
    )
    with pytest.raises(D.Invalid, match="duplicate native occurrence"):
        observed(bundle)


def add_shared_python_rule(
    bundle,
    versions=("1.0", "1.0"),
    severities=("Medium", "Medium"),
    rendered="medium",
    same_locations=False,
):
    root, inputs, _, _ = bundle
    sbom, native, sarif = [
        D.parse((root / inputs[key]).read_bytes())
        for key in ("sbom", "raw_json", "raw_sarif")
    ]
    image_input = "synthetic-image.oci.tar"
    for report in (sbom, native):
        report["source"]["metadata"]["userInput"] = image_input
    run = sarif["runs"][0]
    rule_id = "CVE-2000-0100-pip"
    for index, (version, severity) in enumerate(zip(versions, severities)):
        location_index = 0 if same_locations else index
        path = ["/usr/local/lib/pip/METADATA", "/opt/venv/lib/pip/METADATA"][
            location_index
        ]
        layer = "sha256:" + str(location_index + 1) * 64
        location = {"path": path, "accessPath": path, "layerID": layer}
        artifact = {
            "id": "pip-" + str(index),
            "name": "pip",
            "type": "python",
            "version": version,
            "purl": "pkg:pypi/pip@" + version,
            "locations": [location],
        }
        sbom["artifacts"].append(artifact)
        native["matches"].append(
            {
                "artifact": artifact,
                "vulnerability": {
                    "id": "CVE-2000-0100",
                    "namespace": "github:language:python",
                    "severity": severity,
                },
            }
        )
        run["results"].append(
            {
                "ruleId": rule_id,
                "message": {"text": "Synthetic shared rule"},
                "locations": [
                    {
                        "physicalLocation": {"artifactLocation": {"uri": path}},
                        "logicalLocations": [
                            {
                                "name": path,
                                "fullyQualifiedName": image_input
                                + "@"
                                + layer
                                + ":"
                                + path,
                            }
                        ],
                    }
                ],
            }
        )
    run["tool"]["driver"]["rules"].append(
        {
            "id": rule_id,
            "help": {
                "text": "Vulnerability CVE-2000-0100\nSeverity: "
                + rendered
                + "\nPackage: pip\nVersion: "
                + versions[0]
                + "\nType: python\nData Namespace: github:language:python"
            },
            "properties": {
                "purls": ["pkg:pypi/pip@" + versions[0]],
                "security-severity": "0.0",
            },
        }
    )
    for key, report in [("sbom", sbom), ("raw_json", native), ("raw_sarif", sarif)]:
        (root / inputs[key]).write_bytes(D.canonical(report))


@pytest.mark.parametrize(
    "versions,same_locations",
    [(("1.0", "1.0"), False), (("1.0", "2.0"), False), (("1.0", "1.0"), True)],
)
def test_shared_rules_remain_active_and_keep_all_native_members(
    bundle, versions, same_locations
):
    add_shared_python_rule(bundle, versions=versions, same_locations=same_locations)
    observation = observed(bundle)
    original = D.parse((bundle[0] / bundle[1]["raw_sarif"]).read_bytes())
    record = receipt(bundle, observation)
    output, summary = derive(bundle, record, observation)
    assert summary["native_raw"] == summary["sarif_raw"] == 4
    assert summary["native_active"] == summary["active"] == 3
    assert summary["shared_rule_groups"] == 1
    assert summary["native_raw_severities"] == {"High": 2, "Medium": 2}
    assert output["runs"][0]["results"][2:] == original["runs"][0]["results"][2:]
    assert all(
        not item["disposition_eligible"] for item in observation["occurrences"][2:]
    )


def test_shared_native_member_cannot_be_dispositioned(bundle):
    add_shared_python_rule(bundle)
    observation = observed(bundle)
    record = receipt(bundle, observation)
    record["decisions"][0]["native_sha256"] = observation["occurrences"][2][
        "native_members"
    ][0]["native_sha256"]
    with pytest.raises(D.Invalid, match="ineligible"):
        derive(bundle, record, observation)


@pytest.mark.parametrize("severity", ["High", "Critical"])
def test_shared_rule_cannot_understate_native_high_or_critical(bundle, severity):
    add_shared_python_rule(bundle, severities=("Medium", severity))
    with pytest.raises(D.Invalid, match="understates native severity"):
        observed(bundle)


def test_unknown_rendered_low_preserves_native_unknown_and_remains_ineligible(bundle):
    add_shared_python_rule(bundle, severities=("Unknown", "Unknown"), rendered="low")
    observation = observed(bundle)
    _, summary = derive(bundle, receipt(bundle, observation), observation)
    assert summary["native_raw_severities"]["Unknown"] == 2
    assert summary["native_active_severities"]["Unknown"] == 2
    assert all(
        not item["disposition_eligible"] and item["gate_severity"] == "low"
        for item in observation["occurrences"][2:]
    )


def test_negligible_debian_rendered_low_cannot_be_dispositioned(bundle):
    change_json(
        bundle,
        "raw_json",
        lambda report: report["matches"][1]["vulnerability"].update(
            severity="Negligible"
        ),
    )

    def mutate(report):
        rule = report["runs"][0]["tool"]["driver"]["rules"][1]
        rule["help"]["text"] = rule["help"]["text"].replace(
            "Severity: high", "Severity: low"
        )

    change_json(bundle, "raw_sarif", mutate)
    observation = observed(bundle)
    record = receipt(bundle, observation)
    _, summary = derive(bundle, record, observation)
    assert summary["native_active_severities"] == {"Negligible": 1}
    record["decisions"][0]["native_sha256"] = observation["occurrences"][1][
        "native_sha256"
    ]
    with pytest.raises(D.Invalid, match="ineligible"):
        derive(bundle, record, observation)


def test_shared_rule_missing_native_member_result_denied(bundle):
    add_shared_python_rule(bundle)
    change_json(bundle, "raw_sarif", lambda report: report["runs"][0]["results"].pop())
    with pytest.raises(D.Invalid, match="occurrence count mismatch"):
        observed(bundle)


def test_shared_rule_location_layer_mismatch_denied(bundle):
    add_shared_python_rule(bundle)

    def mutate(report):
        location = report["runs"][0]["results"][2]["locations"][0]["logicalLocations"][
            0
        ]
        location["fullyQualifiedName"] = location["fullyQualifiedName"].replace(
            "sha256:", "different-layer:"
        )

    change_json(bundle, "raw_sarif", mutate)
    with pytest.raises(D.Invalid, match="location multiset mismatch"):
        observed(bundle)


def test_native_sbom_shared_locations_must_agree(bundle):
    add_shared_python_rule(bundle)
    change_json(
        bundle,
        "raw_json",
        lambda report: report["matches"][2]["artifact"]["locations"][0].update(
            path="/different"
        ),
    )
    with pytest.raises(D.Invalid, match="native/SBOM locations mismatch"):
        observed(bundle)


def test_unknown_presenter_version_requires_separate_review(bundle):
    change_json(
        bundle,
        "raw_json",
        lambda report: report["descriptor"].update(version="0.120.0"),
    )
    change_json(
        bundle,
        "raw_sarif",
        lambda report: report["runs"][0]["tool"]["driver"].update(version="0.120.0"),
    )
    with pytest.raises(D.Invalid, match="unsupported SARIF presenter version"):
        observed(bundle)


def test_mismatched_sarif_package_denied(bundle):
    change_json(
        bundle,
        "raw_sarif",
        lambda d: d["runs"][0]["tool"]["driver"]["rules"][0]["properties"].update(
            purls=["pkg:deb/other@1"]
        ),
    )
    with pytest.raises(D.Invalid, match="occurrence mismatch"):
        observed(bundle)


def test_changed_source_revision_denied(bundle):
    with pytest.raises(D.Invalid, match="revision mismatch"):
        D.collect(*bundle[:3], "b" * 40)


def test_index_digest_not_platform_denied(bundle):
    with pytest.raises(D.Invalid, match="missing regular OCI member"):
        D.collect(
            bundle[0], bundle[1], "example.invalid/demo@sha256:" + "f" * 64, bundle[3]
        )


def test_symlinked_bundle_escape_denied(bundle, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "data"
    outside.write_text("outside")
    link = bundle[0] / "escape"
    link.symlink_to(outside)
    with pytest.raises(D.Invalid, match="escapes"):
        D.local_file(bundle[0], "escape")


def test_duplicate_json_keys_denied():
    with pytest.raises(D.Invalid, match="duplicate JSON"):
        D.parse('{"a":1,"a":2}')


def test_symlink_cycle_denied():
    with pytest.raises(D.Invalid, match="symlink cycle"):
        D.resolve_file(
            {
                "/a": {"kind": "symlink", "target": "/b"},
                "/b": {"kind": "symlink", "target": "/a"},
            },
            "/a",
        )


def test_parent_traversal_denied():
    with pytest.raises(D.Invalid, match="traversal"):
        D.normal_path("../../outside")


def test_real_gate_keeps_unreviewed_high_blocking(bundle):
    observation = observed(bundle)
    output, _ = derive(bundle, receipt(bundle, observation), observation)
    code, summary = D.run_gate(output, Path(__file__).parents[2])
    assert code == 1
    assert summary["grype"]["new_count"] == 1


def test_real_gate_passes_only_when_all_synthetic_highs_reviewed(bundle):
    observation = observed(bundle)
    record = receipt(bundle, observation)
    second = copy.deepcopy(record["decisions"][0])
    second["native_sha256"] = observation["occurrences"][1]["native_sha256"]
    record["decisions"].append(second)
    output, _ = derive(bundle, record, observation)
    code, summary = D.run_gate(output, Path(__file__).parents[2])
    assert code == 0
    assert summary["grype"]["new_count"] == 0


def cli_arguments(bundle, record, mode, derived=None):
    root, inputs, image, revision = bundle
    (root / "inputs.json").write_bytes(D.canonical(inputs))
    (root / "review.json").write_bytes(D.canonical(record))
    args = [
        "exact_image_disposition.py",
        mode,
        "--bundle",
        str(root),
        "--image",
        image,
        "--source-revision",
        revision,
        "--approved-receipt-sha256",
        D.object_sha(record),
    ]
    if derived is not None:
        args.extend(["--derived", str(derived)])
    return args


def test_verify_rejects_arbitrary_annotated_sarif(bundle, monkeypatch):
    observation = observed(bundle)
    record = receipt(bundle, observation)
    output, _ = derive(bundle, record, observation)
    output["runs"][0]["results"][1]["suppressions"] = [
        {"kind": "external", "status": "accepted"}
    ]
    path = bundle[0] / "forged.sarif"
    path.write_bytes(D.canonical(output))
    monkeypatch.setattr(D.sys, "argv", cli_arguments(bundle, record, "verify", path))
    assert D.main() == 2


def test_gate_refuses_supplied_annotated_report(bundle, monkeypatch):
    observation = observed(bundle)
    record = receipt(bundle, observation)
    monkeypatch.setattr(
        D.sys,
        "argv",
        cli_arguments(bundle, record, "gate", bundle[0] / "arbitrary.sarif"),
    )
    assert D.main() == 2


def test_derive_does_not_overwrite_raw_report(bundle, monkeypatch):
    observation = observed(bundle)
    record = receipt(bundle, observation)
    raw_path = bundle[0] / bundle[1]["raw_sarif"]
    before = raw_path.read_bytes()
    monkeypatch.setattr(
        D.sys, "argv", cli_arguments(bundle, record, "derive", raw_path)
    )
    assert D.main() == 2
    assert raw_path.read_bytes() == before


def test_verify_accepts_exact_recomputed_derived_report(bundle, monkeypatch):
    observation = observed(bundle)
    record = receipt(bundle, observation)
    output, _ = derive(bundle, record, observation)
    path = bundle[0] / "derived.sarif"
    path.write_bytes(D.canonical(output))
    monkeypatch.setattr(D.sys, "argv", cli_arguments(bundle, record, "verify", path))
    assert D.main() == 0


def test_rebuilt_same_version_package_with_different_bytes_denied(bundle):
    observation = observed(bundle)
    record = receipt(bundle, observation, "fixed")
    root = bundle[0]
    (root / "package-staging/usr/lib/demo.so").write_text("Different harmless bytes\n")
    subprocess.run(
        ["dpkg-deb", "--build", str(root / "package-staging"), str(root / "demo.deb")],
        check=True,
        capture_output=True,
    )
    record["evidence"]["demo.deb"] = D.file_sha(root / "demo.deb")
    with pytest.raises(D.Invalid, match="package bytes differ"):
        derive(bundle, record, observation)


def test_scan_config_cannot_name_another_image(bundle):
    def mutate(report):
        metadata = report["source"]["metadata"]
        config = D.parse(base64.b64decode(metadata["config"]))
        config["config"]["User"] = "999"
        encoded = D.canonical(config)
        metadata["config"] = base64.b64encode(encoded).decode()
        metadata["imageID"] = "sha256:" + D.sha(encoded)

    change_json(bundle, "sbom", mutate)
    with pytest.raises(D.Invalid, match="scan/image config mismatch"):
        observed(bundle)


def test_effective_scanner_configuration_mismatch_denied(bundle):
    change_json(
        bundle, "scanner_config", lambda config: config.update({"only-fixed": True})
    )
    with pytest.raises(D.Invalid, match="scanner config mismatch"):
        observed(bundle)


def test_conflicting_gate_qualitative_security_severity_denied(bundle):
    change_json(
        bundle,
        "raw_sarif",
        lambda d: d["runs"][0]["tool"]["driver"]["rules"][0]["properties"].update(
            {"security-severity": "low"}
        ),
    )
    with pytest.raises(D.Invalid, match="gate-effective severity mismatch"):
        observed(bundle)


def test_conflicting_gate_result_issue_severity_denied(bundle):
    change_json(
        bundle,
        "raw_sarif",
        lambda d: d["runs"][0]["results"][0].update(
            properties={"issue_severity": "low"}
        ),
    )
    with pytest.raises(D.Invalid, match="gate-effective severity mismatch"):
        observed(bundle)


def test_duplicate_debian_purl_cannot_replace_package_identity(bundle):
    def mutate(sbom):
        duplicate = copy.deepcopy(sbom["artifacts"][0])
        duplicate["id"] = "different-package-id"
        sbom["artifacts"].append(duplicate)

    change_json(bundle, "sbom", mutate)
    with pytest.raises(D.Invalid, match="duplicate Debian package PURL"):
        observed(bundle)


def test_decision_package_id_must_equal_mapped_occurrence(bundle):
    observation = observed(bundle)
    package = next(iter(observation["packages"].values()))
    package["package"]["id"] = "different-package-id"
    record = receipt(bundle, observation)
    with pytest.raises(D.Invalid, match="decision package identity mismatch"):
        derive(bundle, record, observation)


def test_hardlink_does_not_dereference_symlink_target():
    files = {
        "/regular": {"kind": "file", "sha256": "f" * 64},
        "/link": {"kind": "symlink", "target": "/regular"},
    }
    with pytest.raises(D.Invalid, match="direct regular file"):
        D.direct_hardlink(files, "/link")
    assert D.direct_hardlink(files, "/regular") == ("/regular", files["/regular"])


def test_hardlink_cannot_traverse_symlink_parent():
    files = {
        "/real/file": {"kind": "file"},
        "/link": {"kind": "symlink", "target": "/real"},
    }
    with pytest.raises(D.Invalid, match="non-directory parent"):
        D.direct_hardlink(files, "/link/file")


def add_embedded_debian_note(bundle, *, same_purl=False, shared=False):
    """Real Syft artifact shape with harmless image bytes and synthetic findings."""
    root, inputs, _, _ = bundle
    reports = {
        key: D.parse((root / inputs[key]).read_bytes())
        for key in ("sbom", "raw_json", "raw_sarif")
    }
    sbom, native, sarif = (reports[key] for key in reports)
    artifact = D.parse(
        (Path(__file__).parent / "fixtures/embedded-debian-elf-note.json").read_bytes()
    )
    if same_purl:
        artifact["version"] = sbom["artifacts"][0]["version"]
        artifact["purl"] = sbom["artifacts"][0]["purl"]
    artifact["locations"][0]["layerID"] = sbom["artifacts"][0]["locations"][0][
        "layerID"
    ]
    image_input = "synthetic-image.oci.tar"
    for report in (sbom, native):
        report["source"]["metadata"]["userInput"] = image_input
    sbom["artifacts"].append(artifact)
    advisory = "CVE-2000-0001" if shared else "CVE-2000-0200"
    native["matches"].append(
        {
            "artifact": copy.deepcopy(artifact),
            "vulnerability": {
                "id": advisory,
                "namespace": "debian:test",
                "severity": "High",
            },
        }
    )
    rule_id = advisory + "-util-linux"
    run = sarif["runs"][0]
    if not shared:
        run["tool"]["driver"]["rules"].append(
            {
                "id": rule_id,
                "help": {
                    "text": f"Vulnerability {advisory}\nData Namespace: debian:test\nPackage: util-linux\nVersion: {artifact['version']}\nType: deb\nSeverity: high"
                },
                "properties": {"purls": [artifact["purl"]], "security-severity": "7.5"},
            }
        )

    def result_location(location):
        path = location["path"]
        return {
            "physicalLocation": {"artifactLocation": {"uri": path}},
            "logicalLocations": [
                {
                    "name": path,
                    "fullyQualifiedName": image_input
                    + "@"
                    + location["layerID"]
                    + ":"
                    + path,
                }
            ],
        }

    run["results"].append(
        {
            "ruleId": rule_id,
            "message": {"text": "Synthetic embedded finding"},
            "locations": [result_location(artifact["locations"][0])],
        }
    )
    if shared:
        run["results"][0]["locations"] = [
            result_location(sbom["artifacts"][0]["locations"][0])
        ]
    for key, report in reports.items():
        (root / inputs[key]).write_bytes(D.canonical(report))
    return artifact


@pytest.mark.parametrize("same_purl", [False, True])
def test_embedded_debian_note_binds_own_bytes_without_root_dpkg_mapping(
    tmp_path, same_purl
):
    bundle = make_bundle(tmp_path, "util-linux", "2.42.4-1+adp131")
    artifact = add_embedded_debian_note(bundle, same_purl=same_purl)
    before = {key: (bundle[0] / name).read_bytes() for key, name in bundle[1].items()}
    observation = observed(bundle)
    assert len(observation["packages"]) == 1
    embedded = observation["embedded_packages"][artifact["id"]]
    assert embedded["package"] == D.package_identity(artifact)
    assert embedded["locations"] == artifact["locations"]
    path = "/opt/awscli-adp/lib/aws-cli/libuuid.so.1"
    assert embedded["files"][path]["sha256"] == D.sha(b"harmless embedded ELF fixture")
    assert all(path not in p["files"] for p in observation["packages"].values())
    occurrence = observation["occurrences"][-1]
    assert not occurrence["disposition_eligible"]
    record = receipt(bundle, observation)
    output, summary = derive(bundle, record, observation)
    assert summary["native_raw"] == 3 and summary["native_active"] == 2
    assert "suppressions" not in output["runs"][0]["results"][-1]
    record["decisions"][0]["native_sha256"] = occurrence["native_sha256"]
    with pytest.raises(D.Invalid, match="ineligible decision"):
        derive(bundle, record, observation)
    for key, name in bundle[1].items():
        assert (bundle[0] / name).read_bytes() == before[key]


def test_embedded_note_shared_with_root_package_keeps_entire_group_active(tmp_path):
    bundle = make_bundle(tmp_path, "util-linux", "2.42.4-1+adp131")
    add_embedded_debian_note(bundle, same_purl=True, shared=True)
    observation = observed(bundle)
    shared = [
        x
        for x in observation["occurrences"]
        if x["rule_id"] == "CVE-2000-0001-util-linux"
    ]
    assert len(shared) == 2 and all(not x["disposition_eligible"] for x in shared)
    assert all(len(x["native_members"]) == 2 for x in shared)
    record = {
        "schema": "adp-exact-image-review/v1",
        "observation_sha256": D.object_sha(observation),
        "reviewer": "SYNTHETIC TEST REVIEWER",
        "decisions": [],
        "evidence": {},
    }
    output, summary = derive(bundle, record, observation)
    assert summary["native_active"] == 3 and summary["shared_rule_groups"] == 1
    assert all("suppressions" not in x for x in output["runs"][0]["results"])


@pytest.mark.parametrize("target", ["native", "sarif"])
def test_embedded_note_location_mismatch_refused(tmp_path, target):
    bundle = make_bundle(tmp_path, "util-linux", "2.42.4-1+adp131")
    add_embedded_debian_note(bundle)
    if target == "native":
        change_json(
            bundle,
            "raw_json",
            lambda x: x["matches"][-1]["artifact"]["locations"][0].update(
                path="/usr/lib/demo.so"
            ),
        )
        reason = "embedded native/SBOM locations mismatch"
    else:
        change_json(
            bundle,
            "raw_sarif",
            lambda x: x["runs"][0]["results"][-1]["locations"][0]["logicalLocations"][
                0
            ].update(name="/usr/lib/demo.so"),
        )
        reason = "location multiset mismatch"
    with pytest.raises(D.Invalid, match=reason):
        observed(bundle)


@pytest.mark.parametrize(
    "field,value",
    [
        ("foundBy", "unknown-cataloger"),
        ("metadataType", "dpkg-db-entry"),
    ],
)
def test_embedded_note_inconsistent_provenance_refused(tmp_path, field, value):
    bundle = make_bundle(tmp_path, "util-linux", "2.42.4-1+adp131")
    add_embedded_debian_note(bundle)
    change_json(bundle, "sbom", lambda x: x["artifacts"][-1].update({field: value}))
    with pytest.raises(D.Invalid, match="unsupported Debian artifact provenance"):
        observed(bundle)


def test_embedded_note_missing_installed_file_refused(tmp_path):
    bundle = make_bundle(tmp_path, "util-linux", "2.42.4-1+adp131")
    artifact = add_embedded_debian_note(bundle)
    artifact["locations"][0]["path"] = "/absent.so"
    with pytest.raises(D.MissingFile):
        D.embedded_inventory(artifact, {})


def test_root_dpkg_version_mismatch_still_refused(bundle):
    change_json(bundle, "sbom", lambda x: x["artifacts"][0].update(version="2.0"))
    change_json(
        bundle,
        "raw_json",
        lambda x: [m["artifact"].update(version="2.0") for m in x["matches"]],
    )
    change_json(
        bundle,
        "raw_sarif",
        lambda x: [
            r["help"].update(
                text=r["help"]["text"].replace("Version: 1.0", "Version: 2.0")
            )
            for r in x["runs"][0]["tool"]["driver"]["rules"]
        ],
    )
    with pytest.raises(D.Invalid, match="SBOM/dpkg package mismatch"):
        observed(bundle)


def test_embedded_high_still_blocks_gate_after_root_findings_reviewed(tmp_path):
    bundle = make_bundle(tmp_path, "util-linux", "2.42.4-1+adp131")
    add_embedded_debian_note(bundle, same_purl=True)
    observation = observed(bundle)
    record = receipt(bundle, observation)
    second = copy.deepcopy(record["decisions"][0])
    second["native_sha256"] = observation["occurrences"][1]["native_sha256"]
    record["decisions"].append(second)
    output, counts = derive(bundle, record, observation)
    assert counts["native_active"] == 1
    code, summary = D.run_gate(output, Path(__file__).parents[2])
    assert code == 1 and summary["grype"]["new_count"] == 1


def test_elf_path_cannot_claim_root_dpkg_provenance(tmp_path):
    bundle = make_bundle(tmp_path, "util-linux", "2.42.4-1+adp131")
    add_embedded_debian_note(bundle)
    change_json(
        bundle,
        "sbom",
        lambda x: x["artifacts"][-1].update(
            foundBy="dpkg-db-cataloger", metadataType="dpkg-db-entry"
        ),
    )
    with pytest.raises(D.Invalid, match="unsupported Debian artifact provenance"):
        observed(bundle)
