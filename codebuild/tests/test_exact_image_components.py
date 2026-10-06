"""Harmless synthetic provider fixtures, never real security decisions."""

import base64
import copy
import csv
import gzip
import importlib.util
import io
import json
import struct
import subprocess
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "component_fixture_helpers",
    Path(__file__).with_name("test_exact_image_disposition.py"),
)
HELPERS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HELPERS)
D, tar_bytes = HELPERS.D, HELPERS.tar_bytes

C = D.component_verifier()


def write_json(root, name, obj):
    (root / name).write_bytes(D.canonical(obj))
    return name


def oci(root, name, files, revision):
    layer = tar_bytes([(p.lstrip("/"), b) for p, b in files.items()])
    cfg = {
        "architecture": "amd64",
        "os": "linux",
        "config": {
            "User": "1000:1000",
            "WorkingDir": "/",
            "Labels": {"org.opencontainers.image.revision": revision},
        },
        "rootfs": {"type": "layers", "diff_ids": ["sha256:" + D.sha(layer)]},
    }
    cfgbytes = D.canonical(cfg)

    def descriptor(b, media):
        return {"digest": "sha256:" + D.sha(b), "size": len(b), "mediaType": media}

    manifest = D.canonical(
        {
            "schemaVersion": 2,
            "config": descriptor(cfgbytes, "application/vnd.oci.image.config.v1+json"),
            "layers": [descriptor(layer, "application/vnd.oci.image.layer.v1.tar")],
        }
    )
    (root / name).write_bytes(
        tar_bytes(
            [
                ("oci-layout", b'{"imageLayoutVersion":"1.0.0"}'),
                *[("blobs/sha256/" + D.sha(b), b) for b in (layer, cfgbytes, manifest)],
            ]
        )
    )
    return (
        "sha256:" + D.sha(manifest),
        "sha256:" + D.sha(cfgbytes),
        "sha256:" + D.sha(layer),
        cfgbytes,
    )


def deb(root, name, payload):
    staging = root / (name + "-staging")
    (staging / "DEBIAN").mkdir(parents=True)
    (staging / "DEBIAN/control").write_text(
        f"Package: {name}\nVersion: 1.0\nArchitecture: amd64\n"
        "Maintainer: Fixture <test@example.invalid>\nDescription: Synthetic test\n"
    )
    for path, content in payload.items():
        out = staging / path.lstrip("/")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(content)
        out.chmod(0o644)
    output = name + ".deb"
    subprocess.run(
        ["dpkg-deb", "--build", str(staging), str(root / output)],
        check=True,
        capture_output=True,
    )
    return output


def elf_fixture(needed=(), soname=None):
    """Non-executable synthetic ELF table bytes for bounded parser tests."""
    strings = b"\x00"
    offsets = []
    for name in needed:
        offsets.append(len(strings))
        strings += name.encode() + b"\x00"
    soname_offset = len(strings)
    if soname:
        strings += soname.encode() + b"\x00"
    dynamic_offset = 64 + 2 * 56
    string_offset = dynamic_offset + 16 * (3 + len(needed) + bool(soname))
    tags = [(5, string_offset), (10, len(strings))] + [(1, off) for off in offsets]
    if soname:
        tags.append((14, soname_offset))
    tags.append((0, 0))
    dynamic = b"".join(struct.pack("<qQ", *entry) for entry in tags)
    size = string_offset + len(strings)
    ident = b"\x7fELF\x02\x01\x01" + bytes(9)
    header = ident + struct.pack(
        "<HHIQQQIHHHHHH", 3, 62, 1, 0, 64, 0, 0, 64, 56, 2, 0, 0, 0
    )
    load = struct.pack("<IIQQQQQQ", 1, 4, 0, 0, 0, size, size, 1)
    segment = struct.pack(
        "<IIQQQQQQ",
        2,
        4,
        dynamic_offset,
        dynamic_offset,
        0,
        len(dynamic),
        len(dynamic),
        1,
    )
    return header + load + segment + dynamic + strings


def make_component(
    root,
    provider="cpython-runtime/v1",
    extra_files=None,
    producer_changes=None,
    extra_advisory=True,
):
    version = "3.10.21" if provider == "cpython-runtime/v1" else "46.0.7+adp1"
    files = {
        C.PYTHON: elf_fixture(["libpython3.10.so.1.0"]),
        C.LIBPYTHON: elf_fixture(["libc.so.6"], "libpython3.10.so.1.0"),
    }
    for name in (
        "site.py",
        "os.py",
        "sysconfig.py",
        "tarfile.py",
        "email/__init__.py",
        "email/utils.py",
        "email/_parseaddr.py",
        "email/parser.py",
        "email/feedparser.py",
        "email/message.py",
        "email/errors.py",
        "email/policy.py",
        "email/_policybase.py",
        "importlib/__init__.py",
        "encodings/__init__.py",
    ):
        files[C.STDLIB + "/" + name] = b"# harmless source\n"
    for name in ("pyexpat", "_elementtree"):
        files[
            C.STDLIB + "/lib-dynload/" + name + ".cpython-310-x86_64-linux-gnu.so"
        ] = elf_fixture(["libexpat.so.1"] if name == "pyexpat" else ["libc.so.6"])
    dist = C.SITE + "/cryptography-46.0.7+adp1.dist-info"
    if provider == "python-installed-distribution/v1":
        files.update(
            {
                dist + "/METADATA": b"Name: cryptography\nVersion: 46.0.7+adp1\n",
                dist + "/WHEEL": (
                    b"Wheel-Version: 1.0\nRoot-Is-Purelib: false\n"
                    b"Tag: cp37-abi3-linux_x86_64\n"
                ),
                dist + "/direct_url.json": b'{"url":"file:///reviewed/source"}',
                C.SITE + "/cryptography/__init__.py": b"# harmless crypto source\n",
                C.SITE + "/cryptography/hazmat/bindings/_rust.abi3.so": elf_fixture(
                    ["libssl.so.3", "libcrypto.so.3"]
                ),
            }
        )
        csvbytes = io.StringIO()
        writer = csv.writer(csvbytes)
        for p, data in files.items():
            if p.startswith(C.SITE + "/"):
                writer.writerow(
                    [
                        p[len(C.SITE) + 1 :],
                        "sha256="
                        + base64.urlsafe_b64encode(bytes.fromhex(D.sha(data)))
                        .decode()
                        .rstrip("="),
                        len(data),
                    ]
                )
        writer.writerow([dist[len(C.SITE) + 1 :] + "/RECORD", "", ""])
        files[dist + "/RECORD"] = csvbytes.getvalue().encode()
    dep_name = "libexpat1" if provider == "cpython-runtime/v1" else "libssl3t64"
    dep_paths = (
        ["/usr/lib/x86_64-linux-gnu/libexpat.so.1"]
        if provider == "cpython-runtime/v1"
        else [
            "/usr/lib/x86_64-linux-gnu/libssl.so.3",
            "/usr/lib/x86_64-linux-gnu/libcrypto.so.3",
        ]
    )
    payload = {p: elf_fixture(["libc.so.6"], p.rsplit("/", 1)[1]) for p in dep_paths}
    files.update(payload)
    files["/var/lib/dpkg/status"] = (
        f"Package: {dep_name}\nStatus: install ok installed\n"
        "Version: 1.0\nArchitecture: amd64\n\n"
    ).encode()
    files["/var/lib/dpkg/info/" + dep_name + ":amd64.list"] = (
        "\n".join(dep_paths) + "\n"
    ).encode()
    files.update(extra_files or {})
    revision, producer_revision = "a" * 40, "b" * 40
    platform, config, layer, cfgbytes = oci(root, "candidate.tar", files, revision)
    producer_files = dict(files)
    producer_files.update(producer_changes or {})
    producer_platform, producer_config, _, _ = oci(
        root, "producer.tar", producer_files, producer_revision
    )
    locations = (
        [C.PYTHON, C.LIBPYTHON]
        if provider == "cpython-runtime/v1"
        else [dist + "/METADATA", dist + "/RECORD", dist + "/direct_url.json"]
    )
    package = {
        "id": "component-id",
        "name": "python" if provider == "cpython-runtime/v1" else "cryptography",
        "version": version,
        "type": "binary" if provider == "cpython-runtime/v1" else "python",
        "purl": "pkg:generic/python@3.10.21"
        if provider == "cpython-runtime/v1"
        else "pkg:pypi/cryptography@46.0.7%2Badp1",
        "foundBy": "binary-classifier-cataloger"
        if provider == "cpython-runtime/v1"
        else "python-installed-package-cataloger",
        "metadataType": "binary-signature"
        if provider == "cpython-runtime/v1"
        else "python-package",
        "locations": [
            {
                "path": p,
                "accessPath": p,
                "layerID": layer,
                "annotations": {"evidence": "primary" if i == 0 else "supporting"},
            }
            for i, p in enumerate(locations)
        ],
    }
    dep_package = {
        "id": "dep-id",
        "name": dep_name,
        "version": "1.0",
        "type": "deb",
        "purl": f"pkg:deb/debian/{dep_name}@1.0?arch=amd64",
        "foundBy": "dpkg-db-cataloger",
        "metadataType": "dpkg-db-entry",
        "metadata": {"architecture": "amd64"},
        "locations": [
            {
                "path": "/var/lib/dpkg/status",
                "accessPath": "/var/lib/dpkg/status",
                "layerID": layer,
                "annotations": {"evidence": "primary"},
            }
        ],
    }
    source = {
        "type": "image",
        "metadata": {
            "imageID": config,
            "config": base64.b64encode(cfgbytes).decode(),
            "userInput": "docker-archive:candidate.tar",
        },
    }
    advisories = sorted(C.ADVISORIES[provider])
    if extra_advisory:
        advisories.append("CVE-2000-9999")
    matches, rules, results = [], [], []
    for advisory in advisories:
        matches.append(
            {
                "artifact": package,
                "vulnerability": {
                    "id": advisory,
                    "namespace": "test:component",
                    "severity": "High",
                },
            }
        )
        rule_id = advisory + "-" + package["name"]
        rules.append(
            {
                "id": rule_id,
                "help": {
                    "text": (
                        f"Vulnerability {advisory}\nData Namespace: test:component\n"
                        f"Package: {package['name']}\nVersion: {version}\n"
                        f"Type: {package['type']}\nSeverity: high"
                    )
                },
                "properties": {"purls": [package["purl"]], "security-severity": "7.5"},
            }
        )
        results.append(
            {
                "ruleId": rule_id,
                "message": {"text": "Synthetic only"},
                "locations": [
                    {
                        "physicalLocation": {"artifactLocation": {"uri": locations[0]}},
                        "logicalLocations": [
                            {
                                "name": p,
                                "fullyQualifiedName": source["metadata"]["userInput"]
                                + "@"
                                + layer
                                + ":"
                                + p,
                            }
                            for p in locations
                        ],
                    }
                ],
            }
        )
    native = {
        "descriptor": {"name": "grype", "version": "0.119.0", "configuration": {}},
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
    inputs = {"archive": "candidate.tar"}
    for key, value in {
        "sbom": {"source": source, "artifacts": [package, dep_package]},
        "raw_json": native,
        "raw_sarif": sarif,
        "scanner_config": {},
        "scanner_binary": {"fixture": True},
        "scanner_database": {"fixture": True},
    }.items():
        inputs[key] = write_json(root, key + ".json", value)
    _, _, inventory, _ = D.collect_oci(root / "candidate.tar", platform)
    owned_deps = {
        p: {
            "package": D.package_identity(dep_package),
            "file": {"resolved": p, **inventory[p]},
            "artifact": dep_name + ".deb",
        }
        for p in dep_paths
    }
    imports = (
        {
            "tarfile": C.STDLIB + "/tarfile.py",
            "email.utils": C.STDLIB + "/email/utils.py",
            "pyexpat": C.STDLIB
            + "/lib-dynload/pyexpat.cpython-310-x86_64-linux-gnu.so",
            "_elementtree": C.STDLIB
            + "/lib-dynload/_elementtree.cpython-310-x86_64-linux-gnu.so",
        }
        if provider == "cpython-runtime/v1"
        else {
            "cryptography": C.SITE + "/cryptography/__init__.py",
            "cryptography.hazmat.bindings._rust": C.SITE
            + "/cryptography/hazmat/bindings/_rust.abi3.so",
        }
    )
    runtime = {
        "schema": "adp-python-runtime-evidence/v1",
        "platform_digest": platform,
        "config_digest": config,
        "source_revision": revision,
        "artifact_id": package["id"],
        "interpreter": {"path": C.PYTHON, "sha256": inventory[C.PYTHON]["sha256"]},
        "imports": {
            n: {"path": p, "sha256": inventory[p]["sha256"]} for n, p in imports.items()
        },
        "libraries": {p: x["file"] for p, x in owned_deps.items()},
        "invocation": "invocation.txt",
        "execution": {
            "argv": [C.PYTHON, "-c", "# harmless synthetic probe"],
            "user": "1000:1000",
            "working_directory": "/",
            "sys_path": [
                "",
                "/usr/local/lib/python310.zip",
                C.STDLIB,
                C.STDLIB + "/lib-dynload",
                C.SITE,
            ],
            "isolated": 0,
            "no_site": 0,
            "environment_overrides": {},
        },
    }
    runtime["libraries"][C.LIBPYTHON] = {
        "resolved": C.LIBPYTHON,
        **inventory[C.LIBPYTHON],
    }
    write_json(root, "runtime.json", runtime)
    for name in ("source.txt", "build.txt", "review.txt", "invocation.txt"):
        (root / name).write_text(
            "Harmless synthetic evidence. No actual build or approval.\n"
        )
    write_json(
        root,
        "review.txt",
        {
            "schema": "adp-producer-source-review/v1",
            "archive_sha256": D.file_sha(root / "producer.tar"),
            "platform_digest": producer_platform,
            "config_digest": producer_config,
            "source_revision": producer_revision,
            "source_evidence": "source.txt",
            "build_evidence": "build.txt",
            "reviewer": "SYNTHETIC SOURCE REVIEWER",
            "conclusion": "source-output-bound",
        },
    )
    binding = {
        "provider": provider,
        "package": D.package_identity(package),
        "producer": {
            "archive": "producer.tar",
            "platform_digest": producer_platform,
            "source_revision": producer_revision,
            "transition": "identical/v1",
        },
        "evidence": {
            "source": "source.txt",
            "build": "build.txt",
            "runtime": "runtime.json",
            "independent_review": "review.txt",
        },
        "dependencies": [
            {
                "package_purl": dep_package["purl"],
                "artifact": deb(root, dep_name, payload),
                "paths": dep_paths,
            }
        ],
    }
    write_json(
        root,
        "components.json",
        {"schema": "adp-python-component-bindings/v1", "bindings": [binding]},
    )
    return root, inputs, "example.invalid/synthetic@" + platform, revision


def collect(bundle):
    return D.collect(*bundle, components="components.json")


def change(bundle, filename, mutate):
    obj = json.loads((bundle[0] / filename).read_text())
    mutate(obj)
    write_json(bundle[0], filename, obj)


def review(bundle, observation, advisory=None):
    component = observation["components"]["component-id"]
    occurrence = next(
        o
        for o in observation["occurrences"]
        if o["disposition_eligible"] and (advisory is None or o["advisory"] == advisory)
    )
    return {
        "schema": "adp-exact-image-review/v2",
        "observation_sha256": D.object_sha(observation),
        "reviewer": "SYNTHETIC REVIEWER",
        "evidence": copy.deepcopy(observation["component_inputs"]),
        "decisions": [
            {
                "native_sha256": occurrence["native_sha256"],
                "status": "fixed",
                "rationale": "Synthetic decision, not a security conclusion",
                "evidence": {
                    role: "review.txt"
                    for role in (
                        "source",
                        "patch",
                        "build",
                        "regression",
                        "independent_review",
                    )
                },
                "files": copy.deepcopy(component["files"]),
                "package_artifact": "producer.tar",
            }
        ],
    }


def derive(bundle, observation, receipt, pin=None):
    return D.derive(
        bundle[0],
        receipt,
        pin or D.object_sha(receipt),
        observation,
        (bundle[0] / bundle[1]["raw_sarif"]).read_bytes(),
    )


@pytest.mark.parametrize("provider", sorted(C.PROVIDERS))
def test_opt_in_collection_and_exact_review(tmp_path, provider):
    bundle = make_component(tmp_path, provider)
    v1 = D.collect(*bundle)
    assert all(not x["disposition_eligible"] for x in v1["occurrences"])
    observation = collect(bundle)
    assert observation["schema"] == "adp-exact-image-observation/v2"
    assert sum(x["disposition_eligible"] for x in observation["occurrences"]) == 3
    raw = (tmp_path / "raw_sarif.json").read_bytes()
    output, summary = derive(bundle, observation, review(bundle, observation))
    assert summary["fixed"] == 1 and summary["active"] == 3
    assert (tmp_path / "raw_sarif.json").read_bytes() == raw
    for result in output["runs"][0]["results"]:
        result.pop("suppressions", None)
    assert output == json.loads(raw)


@pytest.mark.parametrize(
    "extra",
    [
        C.STDLIB + "/__pycache__/tarfile.cpython-310.pyc",
        C.STDLIB + "/tarfile.pyc",
        C.SITE + "/cryptography/__pycache__/__init__.cpython-310.pyc",
    ],
)
def test_bytecode_never_accepted_by_producer_equality(tmp_path, extra):
    provider = (
        "python-installed-distribution/v1"
        if "cryptography" in extra
        else "cpython-runtime/v1"
    )
    bundle = make_component(
        tmp_path,
        provider,
        extra_files={extra: b"timestamp-valid or arbitrary bytecode"},
    )
    with pytest.raises(D.Invalid, match="bytecode|RECORD"):
        collect(bundle)


@pytest.mark.parametrize(
    "path",
    [
        C.SITE + "/unexpected.pth",
        C.STDLIB + "/sitecustomize.py",
        C.SITE + "/usercustomize.py",
    ],
)
def test_unreviewed_import_controls_refused(tmp_path, path):
    bundle = make_component(tmp_path, extra_files={path: b"# harmless hook"})
    with pytest.raises(D.Invalid, match="hook"):
        collect(bundle)


@pytest.mark.parametrize(
    "name",
    [
        "tarfile.py",
        "lib-dynload/pyexpat.cpython-310-x86_64-linux-gnu.so",
        "lib-dynload/_elementtree.cpython-310-x86_64-linux-gnu.so",
    ],
)
def test_all_producer_runtime_code_must_match(tmp_path, name):
    bundle = make_component(
        tmp_path,
        producer_changes={C.STDLIB + "/" + name: b"different source or output"},
    )
    with pytest.raises(D.Invalid, match="complete component scope differs"):
        collect(bundle)


def test_cannot_choose_a_narrower_root(tmp_path):
    bundle = make_component(tmp_path)
    change(
        bundle,
        "components.json",
        lambda m: m["bindings"][0].update(roots=[C.STDLIB + "/tarfile.py"]),
    )
    with pytest.raises(D.Invalid, match="component binding fields"):
        collect(bundle)


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("provider", "generic-binary/v1", "unsupported component provider"),
        ("package", {"id": "wrong"}, "component identity fields"),
    ],
)
def test_unsupported_or_malformed_binding(tmp_path, field, value, reason):
    bundle = make_component(tmp_path)
    change(bundle, "components.json", lambda m: m["bindings"][0].update({field: value}))
    with pytest.raises(D.Invalid, match=reason):
        collect(bundle)


def test_missing_dependency_refused(tmp_path):
    bundle = make_component(tmp_path)
    change(
        bundle, "components.json", lambda m: m["bindings"][0].update(dependencies=[])
    )
    with pytest.raises(D.Invalid, match="incomplete runtime dependency"):
        collect(bundle)


@pytest.mark.parametrize(
    "mutation,reason",
    [
        (lambda r: r["execution"].update(isolated=1), "default import context"),
        (
            lambda r: r["execution"].update(sys_path=["/different"]),
            "module search path",
        ),
        (
            lambda r: r.update(platform_digest="sha256:" + "0" * 64),
            "image/source mismatch",
        ),
        (
            lambda r: r["imports"]["tarfile"].update(path="/other/tarfile.py"),
            "imported code mismatch",
        ),
        (
            lambda r: r["libraries"]["/usr/lib/x86_64-linux-gnu/libexpat.so.1"].update(
                sha256="0" * 64
            ),
            "library binding mismatch",
        ),
    ],
)
def test_runtime_context_evidence_must_match(tmp_path, mutation, reason):
    bundle = make_component(tmp_path)
    change(bundle, "runtime.json", mutation)
    with pytest.raises(D.Invalid, match=reason):
        collect(bundle)


def test_component_decision_cannot_choose_subset(tmp_path):
    bundle = make_component(tmp_path)
    observation = collect(bundle)
    receipt = review(bundle, observation)
    receipt["decisions"][0]["files"].pop(C.STDLIB + "/tarfile.py")
    with pytest.raises(D.Invalid, match="complete owned scope"):
        derive(bundle, observation, receipt)


def test_not_affected_still_requires_complete_provenance(tmp_path):
    bundle = make_component(tmp_path)
    observation = collect(bundle)
    receipt = review(bundle, observation)
    decision = receipt["decisions"][0]
    decision.update(
        status="not_affected",
        package_artifact=None,
        evidence={"independent_review": "review.txt", "applicability": "review.txt"},
    )
    receipt["evidence"].pop("producer.tar")
    with pytest.raises(D.Invalid, match="provenance omitted"):
        derive(bundle, observation, receipt)


@pytest.mark.parametrize("status", ["vendor-disputed", "risk-accepted"])
def test_disputed_and_risk_acceptance_are_not_decisions(tmp_path, status):
    bundle = make_component(tmp_path)
    observation = collect(bundle)
    receipt = review(bundle, observation)
    receipt["decisions"][0]["status"] = status
    with pytest.raises(D.Invalid, match="unsupported decision status"):
        derive(bundle, observation, receipt)


def test_disputed_high_still_blocks_unchanged_gate(tmp_path):
    bundle = make_component(tmp_path, extra_advisory=False)
    observation = collect(bundle)
    receipt = review(bundle, observation, "CVE-2026-7210")
    receipt["decisions"].extend(
        review(bundle, observation, "CVE-2026-82049")["decisions"]
    )
    output, summary = derive(bundle, observation, receipt)
    assert summary["active"] == 1
    result, report = D.run_gate(output, Path(__file__).parents[2])
    assert result == 1 and report["grype"]["new_count"] == 1


def test_changed_component_evidence_and_wrong_external_pin_fail(tmp_path):
    bundle = make_component(tmp_path)
    observation = collect(bundle)
    receipt = review(bundle, observation)
    with pytest.raises(D.Invalid, match="independent approval pin"):
        derive(bundle, observation, receipt, pin="0" * 64)
    (tmp_path / "build.txt").write_text("changed")
    with pytest.raises(D.Invalid, match="modified or missing evidence"):
        derive(bundle, observation, receipt)


def test_only_declared_source_backed_cache_removal_is_allowed(tmp_path):
    cache = C.STDLIB + "/__pycache__/tarfile.cpython-310.pyc"
    bundle = make_component(
        tmp_path, producer_changes={cache: b"unverified historical cache"}
    )
    change(
        bundle,
        "components.json",
        lambda m: m["bindings"][0]["producer"].update(transition="remove-bytecode/v1"),
    )
    observation = collect(bundle)
    transition = observation["components"]["component-id"]["transition"]
    assert transition["removed_caches"][cache]["source"] == C.STDLIB + "/tarfile.py"
    assert cache not in observation["components"]["component-id"]["files"]


def test_orphan_bytecode_not_a_supported_transition(tmp_path):
    bundle = make_component(
        tmp_path,
        producer_changes={C.STDLIB + "/__pycache__/missing.cpython-310.pyc": b"orphan"},
    )
    change(
        bundle,
        "components.json",
        lambda m: m["bindings"][0]["producer"].update(transition="remove-bytecode/v1"),
    )
    with pytest.raises(D.Invalid, match="orphan"):
        collect(bundle)


def test_clean_record_transition_preserves_every_other_byte():
    source = C.SITE + "/cryptography/__init__.py"
    cache = C.SITE + "/cryptography/__pycache__/__init__.cpython-310.pyc"
    record_path = C.SITE + "/cryptography-46.0.7+adp1.dist-info/RECORD"
    raw = (
        b"cryptography/__init__.py,sha256=untouched,19\r\n"
        b"cryptography/__pycache__/__init__.cpython-310.pyc,,\r\n"
        b"cryptography-46.0.7+adp1.dist-info/RECORD,,\r\n"
    )
    files = {
        path: {"kind": "file", "sha256": D.sha(data), "mode": 0o644, "size": len(data)}
        for path, data in [
            (source, b"# source"),
            (cache, b"untrusted old cache"),
            (record_path, raw),
        ]
    }
    clean, contents, changes = C.clean_producer(D, files, {record_path: raw})
    assert cache not in clean and cache in files
    assert contents[record_path] == raw.replace(
        b"cryptography/__pycache__/__init__.cpython-310.pyc,,\r\n", b""
    )
    assert changes["changed_records"][record_path]["before_sha256"] == D.sha(raw)


def test_label_does_not_replace_source_output_review(tmp_path):
    bundle = make_component(tmp_path)
    change(bundle, "review.txt", lambda r: r.update(archive_sha256="0" * 64))
    with pytest.raises(D.Invalid, match="producer review artifact/source mismatch"):
        collect(bundle)


def test_cannot_substitute_candidate_for_producer(tmp_path):
    bundle = make_component(tmp_path)
    change(
        bundle,
        "components.json",
        lambda m: m["bindings"][0]["producer"].update(
            archive="candidate.tar",
            platform_digest=bundle[2].split("@")[1],
            source_revision=bundle[3],
        ),
    )
    with pytest.raises(D.Invalid, match="candidate repack"):
        collect(bundle)


@pytest.mark.parametrize(
    "row,reason",
    [
        (b"../escape,sha256=bad,1\n", "parent traversal"),
        (b"/absolute,sha256=bad,1\n", "invalid RECORD row"),
        (
            b"cryptography-46.0.7+adp1.dist-info/RECORD,,\ncryptography-46.0.7+adp1.dist-info/RECORD,,\n",
            "duplicate RECORD",
        ),
        (
            b"cryptography/__init__.py,,\ncryptography-46.0.7+adp1.dist-info/RECORD,,\n",
            "unhashed",
        ),
        (
            b"cryptography/__init__.py,sha256=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA,0\ncryptography-46.0.7+adp1.dist-info/RECORD,,\n",
            "digest/size mismatch",
        ),
    ],
)
def test_bad_record_never_inherits_producer_approval(tmp_path, row, reason):
    path = C.SITE + "/cryptography-46.0.7+adp1.dist-info/RECORD"
    bundle = make_component(
        tmp_path, "python-installed-distribution/v1", extra_files={path: row}
    )
    with pytest.raises(D.Invalid, match=reason):
        collect(bundle)


def test_record_ownership_overlap_refused(tmp_path):
    neighbor = C.SITE + "/other-1.0.dist-info/RECORD"
    bundle = make_component(
        tmp_path,
        "python-installed-distribution/v1",
        extra_files={neighbor: b"cryptography/__init__.py,,\n"},
    )
    with pytest.raises(D.Invalid, match="overlapping distribution ownership"):
        collect(bundle)


@pytest.mark.parametrize("where", ["native", "sarif", "layer"])
def test_component_locations_must_agree(tmp_path, where):
    bundle = make_component(tmp_path)
    if where == "native":
        change(
            bundle,
            "raw_json.json",
            lambda r: r["matches"][0]["artifact"]["locations"][0].update(
                accessPath="/other"
            ),
        )
        reason = "native/SBOM locations mismatch"
    elif where == "sarif":
        change(
            bundle,
            "raw_sarif.json",
            lambda r: r["runs"][0]["results"][0]["locations"][0]["logicalLocations"][
                0
            ].update(name="/other"),
        )
        reason = "native/SARIF locations mismatch"
    else:
        change(
            bundle,
            "sbom.json",
            lambda r: r["artifacts"][0]["locations"][0].update(
                layerID="sha256:" + "0" * 64
            ),
        )
        reason = "location layer mismatch"
    with pytest.raises(D.Invalid, match=reason):
        collect(bundle)


def test_shared_component_rule_remains_ineligible(tmp_path):
    bundle = make_component(tmp_path)
    sbom = json.loads((tmp_path / "sbom.json").read_text())
    extra = copy.deepcopy(sbom["artifacts"][0])
    extra.update(id="embedded-python", version="3.9.1", purl="pkg:generic/python@3.9.1")
    sbom["artifacts"].append(extra)
    write_json(tmp_path, "sbom.json", sbom)
    native = json.loads((tmp_path / "raw_json.json").read_text())
    match = copy.deepcopy(native["matches"][0])
    match["artifact"] = extra
    native["matches"].append(match)
    write_json(tmp_path, "raw_json.json", native)
    change(
        bundle,
        "raw_sarif.json",
        lambda r: r["runs"][0]["results"].append(
            copy.deepcopy(r["runs"][0]["results"][0])
        ),
    )
    observation = collect(bundle)
    shared = [o for o in observation["occurrences"] if len(o["native_members"]) > 1]
    assert len(shared) == 2 and all(not o["disposition_eligible"] for o in shared)
    receipt = review(bundle, observation)
    receipt["decisions"][0]["native_sha256"] = D.object_sha(match)
    with pytest.raises(D.Invalid, match="ineligible"):
        derive(bundle, observation, receipt)


def test_receipt_schemas_are_disjoint(tmp_path):
    bundle = make_component(tmp_path)
    observation = collect(bundle)
    receipt = review(bundle, observation)
    receipt["schema"] = "adp-exact-image-review/v1"
    with pytest.raises(D.Invalid, match="unsupported receipt"):
        derive(bundle, observation, receipt)


def test_runtime_rejects_missing_parseaddr_even_with_matching_producer(tmp_path):
    bundle = make_component(tmp_path)
    _, _, files, contents = D.collect_oci(
        tmp_path / "candidate.tar", bundle[2].split("@")[1]
    )
    del files[C.STDLIB + "/email/_parseaddr.py"]
    artifact = json.loads((tmp_path / "sbom.json").read_text())["artifacts"][0]
    with pytest.raises(D.Invalid, match="incomplete email parser closure"):
        C.runtime_scope(D, artifact, files, contents)


def test_dynamic_elf_virtual_and_file_mapping_must_agree():
    data = bytearray(elf_fixture(["libc.so.6"]))
    # Second program header's virtual address; keep the file offset unchanged.
    struct.pack_into("<Q", data, 64 + 56 + 16, 177)
    with pytest.raises(D.Invalid, match="virtual/file mapping"):
        C.elf_dynamic(D, data)


@pytest.mark.parametrize(
    "relative", ["unreviewed.py", "licenses/LICENSE.py", "licenses/LICENSE.so"]
)
def test_dist_info_is_not_a_place_for_unreviewed_code(tmp_path, relative):
    bundle = make_component(tmp_path, "python-installed-distribution/v1")
    artifact = json.loads((tmp_path / "sbom.json").read_text())["artifacts"][0]
    _, _, files, contents, _ = C.read_oci(
        D, tmp_path / "candidate.tar", bundle[2].split("@")[1]
    )
    dist = C.SITE + "/cryptography-46.0.7+adp1.dist-info"
    path = dist + "/" + relative
    payload = b"# harmless extra code\n"
    files[path] = {
        "kind": "file",
        "sha256": D.sha(payload),
        "size": len(payload),
        "mode": 0o644,
    }
    encoded = (
        base64.urlsafe_b64encode(bytes.fromhex(D.sha(payload))).decode().rstrip("=")
    )
    contents[dist + "/RECORD"] += (
        f"{path[len(C.SITE) + 1 :]},sha256={encoded},{len(payload)}\n".encode()
    )
    files[dist + "/RECORD"].update(
        sha256=D.sha(contents[dist + "/RECORD"]), size=len(contents[dist + "/RECORD"])
    )
    with pytest.raises(D.Invalid, match="dist-info payload"):
        C.distribution_scope(D, artifact, files, contents)


def test_alias_record_ownership_is_also_ambiguous(tmp_path):
    bundle = make_component(tmp_path, "python-installed-distribution/v1")
    artifact = json.loads((tmp_path / "sbom.json").read_text())["artifacts"][0]
    _, _, files, contents, _ = C.read_oci(
        D, tmp_path / "candidate.tar", bundle[2].split("@")[1]
    )
    files[C.SITE + "/other-alias.py"] = {
        "kind": "symlink",
        "target": "cryptography/__init__.py",
        "mode": 0o777,
    }
    contents[C.SITE + "/other-1.0.dist-info/RECORD"] = b"other-alias.py,,\n"
    with pytest.raises(D.Invalid, match="resolved distribution ownership"):
        C.distribution_scope(D, artifact, files, contents)


def test_future_abi3_minimum_is_not_supported_by_python310(tmp_path):
    dist = C.SITE + "/cryptography-46.0.7+adp1.dist-info"
    bundle = make_component(
        tmp_path,
        "python-installed-distribution/v1",
        extra_files={
            dist + "/WHEEL": (
                b"Wheel-Version: 1.0\nRoot-Is-Purelib: false\n"
                b"Tag: cp311-abi3-linux_x86_64\n"
            )
        },
    )
    with pytest.raises(D.Invalid, match="wheel layout"):
        collect(bundle)


def test_native_dependency_cannot_be_asserted_only_in_runtime_json(tmp_path):
    bundle = make_component(
        tmp_path,
        extra_files={
            C.STDLIB
            + "/lib-dynload/pyexpat.cpython-310-x86_64-linux-gnu.so": elf_fixture(
                ["libc.so.6"]
            )
        },
    )
    with pytest.raises(D.Invalid, match="Expat ABI"):
        collect(bundle)


def test_compressed_blob_is_verified_but_locations_bind_diffid(tmp_path):
    layer = tar_bytes([("native-file", b"harmless data")])
    compressed = gzip.compress(layer, mtime=0)
    config = D.canonical(
        {
            "os": "linux",
            "architecture": "amd64",
            "rootfs": {"diff_ids": ["sha256:" + D.sha(layer)]},
        }
    )
    manifest = D.canonical(
        {
            "schemaVersion": 2,
            "config": {"digest": "sha256:" + D.sha(config), "size": len(config)},
            "layers": [
                {
                    "digest": "sha256:" + D.sha(compressed),
                    "size": len(compressed),
                    "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                }
            ],
        }
    )
    archive = tmp_path / "compressed.tar"
    archive.write_bytes(
        tar_bytes(
            [
                ("oci-layout", b'{"imageLayoutVersion":"1.0.0"}'),
                *[
                    ("blobs/sha256/" + D.sha(b), b)
                    for b in (config, manifest, compressed)
                ],
            ]
        )
    )
    layers = {}
    D.collect_oci(archive, "sha256:" + D.sha(manifest), file_layers=layers)
    assert layers["/native-file"] == "sha256:" + D.sha(layer)
    assert layers["/native-file"] != "sha256:" + D.sha(compressed)
