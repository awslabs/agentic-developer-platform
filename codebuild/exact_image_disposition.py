#!/usr/bin/env python3
"""Collect and verify exact-image review receipts; never modify raw reports.

Deliberately not wired into release workflows. An approval digest is an external
trust input, never read from the candidate bundle. No risk-acceptance status.
"""

from __future__ import annotations

import argparse
import base64
import copy
import gzip
import hashlib
import importlib.util
import io
import json
import posixpath
import re
import subprocess
import sys
import tarfile
import tempfile
from collections import Counter
from functools import lru_cache
from pathlib import Path


class Invalid(ValueError):
    pass


class MissingFile(Invalid):
    def __init__(self, path):
        self.path = path
        super().__init__("missing installed file: " + path)


def require(condition, message):
    if not condition:
        raise Invalid(message)


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def object_sha(value):
    return sha(canonical(value))


@lru_cache(maxsize=1)
def maintained_gate():
    path = (
        Path(__file__).resolve().parents[1]
        / ".github/scripts/diff_security_findings.py"
    )
    spec = importlib.util.spec_from_file_location("adp_maintained_security_gate", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def file_sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f"duplicate JSON key: {key}")
        result[key] = value
    return result


def parse(data):
    return json.loads(data, object_pairs_hook=unique_object)


def exact_keys(value, keys, where):
    require(
        isinstance(value, dict) and set(value) == set(keys), f"invalid {where} fields"
    )


def local_file(root, name):
    require(
        isinstance(name, str) and name and not Path(name).is_absolute(),
        "relative artifact path required",
    )
    resolved = (root / name).resolve(strict=True)
    require(
        resolved.is_relative_to(root.resolve()) and resolved.is_file(),
        "artifact escapes bundle",
    )
    return resolved


def normal_path(name):
    require(isinstance(name, str) and "\x00" not in name, "invalid image path")
    name = name.removeprefix("./").lstrip("/")
    require(".." not in name.split("/"), "parent traversal in image path")
    return "/" + posixpath.normpath(name).lstrip("/")


def validate_parents(files, path):
    parents = path.strip("/").split("/")[:-1]
    for index in range(len(parents)):
        entry = files.get("/" + "/".join(parents[: index + 1]))
        require(
            entry is None or entry["kind"] == "directory",
            "unsupported write through non-directory parent",
        )
    return parents


def direct_hardlink(files, target):
    path = normal_path(target)
    validate_parents(files, path)
    entry = files.get(path)
    require(
        entry is not None and entry["kind"] == "file",
        "hardlink target must be a direct regular file",
    )
    return path, entry


def resolve_file(files, path):
    path = normal_path(path)
    for _ in range(40):
        parts = path.strip("/").split("/")
        for index in range(len(parts)):
            prefix = "/" + "/".join(parts[: index + 1])
            entry = files.get(prefix)
            if entry and entry["kind"] == "symlink":
                target = entry["target"]
                destination = (
                    target
                    if target.startswith("/")
                    else posixpath.join(posixpath.dirname(prefix), target)
                )
                path = normal_path(
                    posixpath.normpath(posixpath.join(destination, *parts[index + 1 :]))
                )
                break
            require(
                entry is None
                or index == len(parts) - 1
                or entry["kind"] == "directory",
                "non-directory path ancestor",
            )
        else:
            if path not in files:
                raise MissingFile(path)
            return path, files[path]
    raise Invalid("symlink cycle or excessive depth")


def collect_oci(archive, platform_digest):
    """Verify OCI blobs and reconstruct file hashes, never extract archive paths.

    OCI tar and gzip/identity layers are supported. Unknown compression, devices,
    writes through symlink parents, duplicate layer paths and dangling hardlinks
    fail closed. Refusing an unsupported archive is preferable to guessing its
    effective filesystem. Hardlinks capture target bytes at creation time.
    """
    require(
        re.fullmatch(r"sha256:[0-9a-f]{64}", platform_digest),
        "immutable platform digest required",
    )
    files = {}
    retained = {}
    # A conservative set of prefixes ever populated. This avoids scanning every
    # existing file for each new regular file while preserving subtree removal.
    populated_prefixes = set()
    with tarfile.open(archive, "r:*") as outer:
        members = {}
        for member in outer:
            path = normal_path(member.name)
            require(path not in members, "duplicate OCI archive member")
            members[path] = member

        def read_member(path):
            member = members.get(path)
            require(
                member is not None and member.isfile(),
                f"missing regular OCI member: {path}",
            )
            return outer.extractfile(member).read()

        require(
            parse(read_member("/oci-layout")).get("imageLayoutVersion") == "1.0.0",
            "unsupported OCI layout",
        )

        def blob(descriptor):
            digest = descriptor["digest"]
            require(
                re.fullmatch(r"sha256:[0-9a-f]{64}", digest), "unsupported blob digest"
            )
            data = read_member("/blobs/sha256/" + digest.split(":")[1])
            require(
                len(data) == descriptor["size"] and "sha256:" + sha(data) == digest,
                "OCI blob mismatch",
            )
            return data

        # The expected platform blob is the trust input. An index is not silently
        # treated as a platform or a config digest.
        manifest_bytes = read_member("/blobs/sha256/" + platform_digest.split(":")[1])
        require(
            "sha256:" + sha(manifest_bytes) == platform_digest,
            "platform manifest mismatch",
        )
        manifest = parse(manifest_bytes)
        require(
            manifest.get("schemaVersion") == 2
            and "layers" in manifest
            and "config" in manifest,
            "expected platform manifest, not index",
        )
        config_bytes = blob(manifest["config"])
        config = parse(config_bytes)
        require(config.get("os") == "linux", "only Linux images supported")
        diff_ids = config["rootfs"]["diff_ids"]
        require(len(diff_ids) == len(manifest["layers"]), "layer/config count mismatch")
        for descriptor, diff_id in zip(manifest["layers"], diff_ids):
            compressed = blob(descriptor)
            media = descriptor["mediaType"]
            if media.endswith("+gzip") or media.endswith(".gzip"):
                data = gzip.decompress(compressed)
            else:
                require(media.endswith(".tar"), "unsupported layer compression")
                data = compressed
            require(
                "sha256:" + sha(data) == diff_id, "uncompressed layer digest mismatch"
            )
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as layer:
                entries = layer.getmembers()
                names = [normal_path(entry.name) for entry in entries]
                require(len(names) == len(set(names)), "duplicate path in layer")
                # Whiteouts apply to lower layers before current-layer additions.
                for member, name in zip(entries, names):
                    leaf = posixpath.basename(name)
                    if leaf.startswith(".wh."):
                        validate_parents(files, name)
                        require(
                            member.isfile() and member.size == 0,
                            "unsupported whiteout representation",
                        )
                        parent = posixpath.dirname(name)
                        target = (
                            parent
                            if leaf == ".wh..wh..opq"
                            else posixpath.join(parent, leaf[4:])
                        )
                        for key in list(files):
                            if key.startswith(target.rstrip("/") + "/") or (
                                leaf != ".wh..wh..opq" and key == target
                            ):
                                files.pop(key)
                                retained.pop(key, None)
                for member, name in zip(entries, names):
                    if posixpath.basename(name).startswith(".wh."):
                        continue
                    parents = validate_parents(files, name)
                    if not member.isdir() and name in populated_prefixes:
                        for key in list(files):
                            if key.startswith(name.rstrip("/") + "/"):
                                files.pop(key)
                                retained.pop(key, None)
                    populated_prefixes.update(
                        "/" + "/".join(parents[: i + 1]) for i in range(len(parents))
                    )
                    retained.pop(name, None)
                    if member.isfile():
                        content = layer.extractfile(member).read()
                        files[name] = {
                            "kind": "file",
                            "sha256": sha(content),
                            "mode": member.mode,
                            "size": len(content),
                        }
                        if name == "/var/lib/dpkg/status" or (
                            name.startswith("/var/lib/dpkg/info/")
                            and name.endswith(".list")
                        ):
                            retained[name] = content
                    elif member.isdir():
                        files[name] = {"kind": "directory", "mode": member.mode}
                    elif member.issym():
                        files[name] = {
                            "kind": "symlink",
                            "target": member.linkname,
                            "mode": member.mode,
                        }
                    elif member.islnk():
                        target, entry = direct_hardlink(files, member.linkname)
                        files[name] = copy.deepcopy(entry)
                        if target in retained:
                            retained[name] = retained[target]
                    else:
                        raise Invalid("unsupported special file in layer")
    return config, manifest["config"]["digest"], files, retained


INPUT_KEYS = {
    "archive",
    "sbom",
    "raw_json",
    "raw_sarif",
    "scanner_binary",
    "scanner_database",
    "scanner_config",
}


def package_identity(package):
    return {key: package[key] for key in ("id", "name", "version", "type", "purl")}


def dpkg_status(content):
    packages = {}
    for paragraph in content.decode().strip().split("\n\n"):
        fields = {}
        for line in paragraph.splitlines():
            if line and not line[0].isspace() and ": " in line:
                key, value = line.split(": ", 1)
                require(key not in fields, "duplicate dpkg status field")
                fields[key] = value
        if fields.get("Status") != "install ok installed":
            continue
        key = (fields["Package"], fields["Architecture"])
        require(key not in packages, "duplicate installed package")
        packages[key] = fields
    return packages


def collect(root, inputs, image, source_revision):
    exact_keys(inputs, INPUT_KEYS, "inputs")
    require(
        re.fullmatch(r"[^@\s]+@sha256:[0-9a-f]{64}", image),
        "repository@platform-digest required",
    )
    require(
        re.fullmatch(r"[0-9a-f]{40}", source_revision), "full source revision required"
    )
    paths = {key: local_file(root, value) for key, value in inputs.items()}
    hashes = {key: file_sha(path) for key, path in paths.items()}
    platform = image.split("@")[1]
    config, config_digest, files, retained = collect_oci(paths["archive"], platform)
    require(
        config.get("config", {})
        .get("Labels", {})
        .get("org.opencontainers.image.revision")
        == source_revision,
        "image source revision mismatch",
    )
    sbom, native, sarif = (
        parse(paths[key].read_bytes()) for key in ("sbom", "raw_json", "raw_sarif")
    )
    # OCI config serialization is not canonical; verify source embedded bytes
    # and compare parsed contents instead of substituting a canonical digest.
    for report in (sbom, native):
        source = report["source"]
        metadata = source.get("metadata", source.get("target", {}))
        require(source.get("type") == "image", "scan source is not an image")
        embedded = base64.b64decode(metadata["config"], validate=True)
        require(parse(embedded) == config, "scan/image config mismatch")
        require(
            "sha256:" + sha(embedded) == config_digest, "scan config bytes mismatch"
        )
        require(metadata["imageID"] == config_digest, "scan imageID mismatch")
    # Require the actual config bytes, not merely semantically equivalent JSON.
    with tarfile.open(paths["archive"], "r:*") as archive:
        matches = [
            m
            for m in archive
            if normal_path(m.name) == "/blobs/sha256/" + config_digest.split(":")[1]
        ]
        require(
            len(matches) == 1 and matches[0].isfile(),
            "scan config blob absent from OCI archive",
        )
        require(
            sha(archive.extractfile(matches[0]).read()) == config_digest.split(":")[1],
            "scan config hash mismatch",
        )
    descriptor = native["descriptor"]
    require(descriptor["name"] == "grype", "not a Grype native report")
    require(
        not native.get("ignoredMatches"), "raw native report already ignores findings"
    )
    require(
        parse(paths["scanner_config"].read_bytes()) == descriptor["configuration"],
        "effective scanner config mismatch",
    )
    # This pin proves which database bytes were approved. The scanner invocation
    # must also be reviewed: a report's self-asserted metadata is not attestation.
    mappings = map_findings(sbom, native, sarif)
    installed = dpkg_status(retained.get("/var/lib/dpkg/status", b""))
    inventories = {}
    for artifact in sbom["artifacts"]:
        if artifact.get("type") != "deb":
            continue
        identity = package_identity(artifact)
        require(identity["purl"] not in inventories, "duplicate Debian package PURL")
        architecture = artifact.get("metadata", {}).get("architecture")
        candidates = [
            p
            for (name, arch), p in installed.items()
            if name == identity["name"]
            and (architecture is None or arch == architecture)
        ]
        require(
            len(candidates) == 1 and candidates[0]["Version"] == identity["version"],
            "SBOM/dpkg package mismatch",
        )
        package = candidates[0]
        list_paths = [
            "/var/lib/dpkg/info/" + identity["name"] + suffix + ".list"
            for suffix in ("", ":" + package["Architecture"])
        ]
        found = [path for path in list_paths if path in retained]
        require(len(found) == 1, "missing or ambiguous package-owned file list")
        owned = {}
        for path in retained[found[0]].decode().splitlines():
            normalized = normal_path(path)
            if normalized in ("/", "/."):
                continue
            try:
                resolved, entry = resolve_file(files, normalized)
                owned[normalized] = {"resolved": resolved, **entry}
            except MissingFile as error:
                # Minimal container images legitimately remove package-owned
                # docs/manpages. Preserve absence rather than omit the path.
                owned[normalized] = {"resolved": error.path, "kind": "absent"}
        inventories[identity["purl"]] = {
            "package": identity,
            "dpkg": package,
            "files": owned,
        }
    require(
        hashes == {key: file_sha(path) for key, path in paths.items()},
        "input changed during collection",
    )
    return {
        "schema": "adp-exact-image-observation/v1",
        "image": image,
        "platform_digest": platform,
        "config_digest": config_digest,
        "architecture": config["architecture"],
        "source_revision": source_revision,
        "inputs": hashes,
        "files_sha256": object_sha(files),
        "packages": inventories,
        "occurrences": mappings,
    }


def help_field(text, field):
    values = re.findall(r"^" + re.escape(field) + r": (.*)$", text, re.MULTILINE)
    require(len(values) == 1 and values[0], "missing or ambiguous SARIF " + field)
    return values[0]


def map_findings(sbom, native, sarif):
    require(
        sarif.get("version") == "2.1.0" and len(sarif.get("runs", [])) == 1,
        "single SARIF 2.1.0 run required",
    )
    run = sarif["runs"][0]
    require(run["tool"]["driver"]["name"].lower() == "grype", "not Grype SARIF")
    require(
        run["tool"]["driver"].get("version") == native["descriptor"]["version"],
        "scanner version mismatch",
    )
    artifacts = {}
    for item in sbom["artifacts"]:
        require(item["id"] not in artifacts, "duplicate SBOM artifact id")
        artifacts[item["id"]] = item
    by_key = {}
    for match in native["matches"]:
        artifact = match["artifact"]
        require(
            artifact["id"] in artifacts
            and package_identity(artifact)
            == package_identity(artifacts[artifact["id"]]),
            "native/SBOM package identity mismatch",
        )
        vulnerability = match["vulnerability"]
        key = (vulnerability["id"], vulnerability["namespace"], artifact["purl"])
        require(key not in by_key, "ambiguous native occurrence")
        by_key[key] = match
    rules = {}
    for rule in run["tool"]["driver"]["rules"]:
        require(rule["id"] not in rules, "duplicate SARIF rule")
        rules[rule["id"]] = rule
    mappings, seen = [], set()
    for index, result in enumerate(run["results"]):
        require(
            not result.get("suppressions"),
            "raw SARIF contains unexplained suppressions",
        )
        rule = rules[result["ruleId"]]
        purls = rule.get("properties", {}).get("purls", [])
        require(len(purls) == 1, "ambiguous SARIF package PURL")
        text = rule["help"]["text"]
        first = re.findall(r"^Vulnerability (\S+)$", text, re.MULTILINE)
        require(len(first) == 1, "missing SARIF advisory identity")
        key = (first[0], help_field(text, "Data Namespace"), purls[0])
        require(
            key in by_key and key not in seen,
            "SARIF/native occurrence mismatch or duplicate",
        )
        seen.add(key)
        match = by_key[key]
        artifact = match["artifact"]
        for field, source in [
            ("Package", "name"),
            ("Version", "version"),
            ("Type", "type"),
        ]:
            require(
                help_field(text, field) == artifact[source],
                "SARIF package metadata mismatch",
            )
        require(
            help_field(text, "Severity").lower()
            == match["vulnerability"]["severity"].lower(),
            "severity mismatch",
        )
        effective_severity, _ = maintained_gate().resolve_sarif_severity(result, rules)
        require(
            effective_severity == match["vulnerability"]["severity"].lower(),
            "gate-effective severity mismatch",
        )
        mappings.append(
            {
                "result_index": index,
                "result_sha256": object_sha(result),
                "native_sha256": object_sha(match),
                "package": package_identity(artifact),
                "advisory": key[0],
                "namespace": key[1],
                "severity": match["vulnerability"]["severity"],
            }
        )
    require(seen == set(by_key), "SARIF omitted native findings")
    return mappings


def verify_deb(path, package, selected_files):
    """Inspect package metadata/data without installing or executing scripts."""
    control = subprocess.run(
        ["dpkg-deb", "--field", str(path), "Package", "Version", "Architecture"],
        capture_output=True,
        text=True,
        check=False,
    )
    require(control.returncode == 0, "fixed artifact is not a readable Debian package")
    fields = dict(line.split(": ", 1) for line in control.stdout.strip().splitlines())
    require(
        fields
        == {
            key: package["dpkg"][key] for key in ("Package", "Version", "Architecture")
        },
        "fixed package metadata differs from installed package",
    )
    payload = subprocess.run(
        ["dpkg-deb", "--fsys-tarfile", str(path)], capture_output=True, check=False
    )
    require(payload.returncode == 0, "cannot inspect fixed package payload")
    files = {}
    with tarfile.open(fileobj=io.BytesIO(payload.stdout), mode="r:") as archive:
        for member in archive:
            name = normal_path(member.name)
            require(name not in files, "duplicate package payload path")
            validate_parents(files, name)
            if member.isfile():
                content = archive.extractfile(member).read()
                files[name] = {
                    "kind": "file",
                    "sha256": sha(content),
                    "mode": member.mode,
                    "size": len(content),
                }
            elif member.isdir():
                files[name] = {"kind": "directory", "mode": member.mode}
            elif member.issym():
                files[name] = {
                    "kind": "symlink",
                    "target": member.linkname,
                    "mode": member.mode,
                }
            elif member.islnk():
                _, target = direct_hardlink(files, member.linkname)
                files[name] = copy.deepcopy(target)
            else:
                raise Invalid("unsupported package special file")
    for name, expected in selected_files.items():
        resolved, entry = resolve_file(files, name)
        require(
            {"resolved": resolved, **entry} == expected,
            "fixed package bytes differ from installed evidence",
        )


def derive(root, receipt, approval_sha256, observation, raw_sarif):
    require(
        re.fullmatch(r"[0-9a-f]{64}", approval_sha256 or ""),
        "external approval pin required",
    )
    require(
        object_sha(receipt) == approval_sha256,
        "receipt does not match independent approval pin",
    )
    exact_keys(
        receipt,
        {"schema", "observation_sha256", "reviewer", "decisions", "evidence"},
        "receipt",
    )
    require(receipt["schema"] == "adp-exact-image-review/v1", "unsupported receipt")
    require(
        isinstance(receipt["reviewer"], str) and receipt["reviewer"].strip(),
        "independent reviewer identity required",
    )
    require(
        receipt["observation_sha256"] == object_sha(observation),
        "cross-image or artifact binding mismatch",
    )
    for name, expected in receipt["evidence"].items():
        require(
            re.fullmatch(r"[0-9a-f]{64}", expected)
            and file_sha(local_file(root, name)) == expected,
            "modified or missing evidence",
        )
    # Recompute the raw report linkage even if this API is called without CLI.
    require(
        sha(raw_sarif) == observation["inputs"]["raw_sarif"],
        "raw SARIF binding mismatch",
    )
    output = parse(raw_sarif)
    results = output["runs"][0]["results"]
    require(
        all(not result.get("suppressions") for result in results),
        "preexisting raw suppressions",
    )
    occurrences = {item["native_sha256"]: item for item in observation["occurrences"]}
    require(
        len(occurrences) == len(observation["occurrences"]) == len(results),
        "ambiguous occurrence inventory",
    )
    seen, counts = set(), Counter()
    for decision in receipt["decisions"]:
        exact_keys(
            decision,
            {
                "native_sha256",
                "status",
                "rationale",
                "evidence",
                "files",
                "package_artifact",
            },
            "decision",
        )
        key = decision["native_sha256"]
        require(key in occurrences and key not in seen, "unknown or duplicate decision")
        seen.add(key)
        require(
            decision["status"] in ("fixed", "not_affected"),
            "unsupported decision status",
        )
        require(
            isinstance(decision["rationale"], str) and decision["rationale"].strip(),
            "decision rationale required",
        )
        roles = (
            {"independent_review", "source", "patch", "regression", "build"}
            if decision["status"] == "fixed"
            else {"independent_review", "applicability"}
        )
        exact_keys(decision["evidence"], roles, "decision evidence roles")
        require(
            all(name in receipt["evidence"] for name in decision["evidence"].values()),
            "retained decision evidence required",
        )
        if decision["status"] == "fixed":
            require(
                decision["package_artifact"] in receipt["evidence"],
                "fixed package artifact required",
            )
        else:
            require(
                decision["package_artifact"] is None,
                "not-affected decision must not imply package replacement",
            )
        occurrence = occurrences[key]
        package = observation["packages"].get(occurrence["package"]["purl"])
        require(
            package is not None,
            "only verified Debian package decisions currently supported",
        )
        require(
            package["package"] == occurrence["package"],
            "decision package identity mismatch",
        )
        require(decision["files"], "exact installed-file evidence required")
        for path, expected in decision["files"].items():
            require(
                path in package["files"] and package["files"][path] == expected,
                "installed-file binding mismatch",
            )
        if decision["status"] == "fixed":
            artifact = local_file(root, decision["package_artifact"])
            verify_deb(artifact, package, decision["files"])
            require(
                file_sha(artifact) == receipt["evidence"][decision["package_artifact"]],
                "package changed during inspection",
            )
        result = results[occurrence["result_index"]]
        require(
            object_sha(result) == occurrence["result_sha256"],
            "result mutation or index mismatch",
        )
        result["suppressions"] = [
            {
                "kind": "external",
                "status": "accepted",
                "justification": decision["status"]
                + ": "
                + decision["rationale"]
                + "; approved receipt sha256:"
                + approval_sha256,
            }
        ]
        counts[decision["status"]] += 1
    summary = {
        "raw": len(results),
        "fixed": counts["fixed"],
        "not_affected": counts["not_affected"],
        "active": len(results) - len(seen),
        "receipt_sha256": approval_sha256,
        "observation_sha256": object_sha(observation),
        "derived_sarif_sha256": object_sha(output),
        "image": observation["image"],
    }
    return output, summary


def run_gate(output, repository):
    """Gate only the freshly derived object, never an arbitrary supplied SARIF.

    This release entry point invokes the maintained gate and its current empty
    baseline. The repository containing this script must itself be trusted.
    """
    baseline = repository / ".github/security/grype-baseline.json"
    baseline_data = parse(baseline.read_bytes())
    require(
        baseline_data.get("version") == "2.1.0" and baseline_data.get("runs"),
        "invalid maintained baseline",
    )
    require(
        all(run.get("results") == [] for run in baseline_data["runs"]),
        "exact-image gate requires empty maintained baseline",
    )
    with tempfile.TemporaryDirectory(prefix="adp-exact-image-gate-") as directory:
        root = Path(directory)
        findings = root / "findings"
        findings.mkdir()
        (findings / "grype-derived.sarif").write_bytes(canonical(output))
        summary_path = root / "summary.json"
        result = subprocess.run(
            [
                sys.executable,
                str(repository / ".github/scripts/diff_security_findings.py"),
                "--findings-dir",
                str(findings),
                "--baseline-dir",
                str(baseline.parent),
                "--output",
                str(summary_path),
                "--fail-on",
                "critical,high",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        require(
            result.returncode in (0, 1) and summary_path.is_file(),
            "maintained security gate failed to execute",
        )
        return result.returncode, parse(summary_path.read_bytes())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("collect", "derive", "verify", "gate"))
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--inputs", default="inputs.json")
    parser.add_argument(
        "--image",
        required=True,
        help="independently selected repository@platform SHA256",
    )
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--receipt", default="review.json")
    parser.add_argument(
        "--approved-receipt-sha256",
        help="independently supplied trust pin; never copied from bundle",
    )
    parser.add_argument(
        "--derived",
        type=Path,
        help="existing derived SARIF to verify, or new output for derive",
    )
    args = parser.parse_args()
    try:
        root = args.bundle.resolve(strict=True)
        inputs = parse(local_file(root, args.inputs).read_bytes())
        observation = collect(root, inputs, args.image, args.source_revision)
        if args.mode == "collect":
            print(json.dumps(observation, indent=2))
            return 0
        receipt = parse(local_file(root, args.receipt).read_bytes())
        output, summary = derive(
            root,
            receipt,
            args.approved_receipt_sha256,
            observation,
            local_file(root, inputs["raw_sarif"]).read_bytes(),
        )
        if args.mode == "gate":
            require(
                args.derived is None,
                "gate regenerates SARIF; supplied annotated reports are not accepted",
            )
            code, gate_summary = run_gate(output, Path(__file__).resolve().parents[1])
            summary["gate"] = gate_summary
            summary["gate_exit_code"] = code
            print(json.dumps(summary, indent=2))
            return code
        require(args.derived is not None, "--derived required")
        if args.mode == "verify":
            require(
                parse(args.derived.read_bytes()) == output,
                "derived SARIF has unauthorized modifications",
            )
        else:
            # Exclusive creation prevents clobbering raw reports or stale output.
            with args.derived.open("x") as stream:
                json.dump(output, stream, indent=2)
                stream.write("\n")
        print(json.dumps(summary, indent=2))
        return 0
    except (
        Invalid,
        KeyError,
        TypeError,
        ValueError,
        OSError,
        tarfile.TarError,
    ) as error:
        print("Exact-image review refused: " + str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
