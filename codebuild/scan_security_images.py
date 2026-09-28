"""Build or pull the selected images, scan them, and publish explicit coverage."""

import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import yaml
from security_image_targets import discover


def command(args, **kwargs):
    print("+ " + " ".join(map(str, args)), flush=True)
    return subprocess.run(args, check=True, **kwargs)


def prepare(target, root):
    for step in target.get("prepare", []):
        if not isinstance(step, list) or not step:
            raise ValueError(f"Invalid preparation step for {target['name']}")
        if step[0] == "copy-tree" and len(step) == 3:
            source = root / step[1]
            destination = root / step[2]
            if not source.is_dir():
                raise ValueError(
                    f"Missing preparation source for {target['name']}: {step[1]}"
                )
            shutil.rmtree(destination, ignore_errors=True)
            shutil.copytree(source, destination)
        elif step[0] == "run" and len(step) > 1:
            command(step[1:], cwd=root)
        else:
            raise ValueError(f"Invalid preparation step for {target['name']}: {step}")


def authenticate_registry(image):
    """Authenticate private ECR pulls without placing the token in logs/argv."""
    registry = image.split("/", 1)[0]
    match = re.fullmatch(
        r"[0-9]{12}\.dkr\.ecr\.([a-z0-9-]+)\.amazonaws\.com(?:\.cn)?", registry
    )
    if match is None:
        return
    password = command(
        ["aws", "ecr", "get-login-password", "--region", match.group(1)],
        stdout=subprocess.PIPE,
        text=True,
        timeout=60,
    ).stdout
    command(
        ["docker", "login", "--username", "AWS", "--password-stdin", registry],
        input=password,
        text=True,
        stdout=subprocess.DEVNULL,
        timeout=60,
    )


def authenticate_dockerfile(path):
    """Log in for literal private FROM/COPY/ARG references as well as build args."""
    # Match only AWS registry hostnames; no credential is sent to arbitrary hosts.
    # Docker expands supplied build args separately (authenticated by scan()).
    source = "\n".join(
        line for line in path.read_text().splitlines()
        if not line.lstrip().startswith("#")
    )
    registries = set(re.findall(
        r"(?<![A-Za-z0-9.-])([0-9]{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com(?:\.cn)?)/",
        source,
    ))
    for registry in sorted(registries):
        authenticate_registry(registry + "/")


def scanner_metadata(descriptor_path, output):
    """Publish only matching policy and DB provenance, never registry configuration."""
    document = json.loads(descriptor_path.read_text())
    descriptor = document.get("descriptor") if isinstance(document, dict) else None
    if not isinstance(descriptor, dict) or not isinstance(descriptor.get("db"), dict):
        raise ValueError("Missing or invalid Grype scanner/DB descriptor")  # noqa: TRY004 - malformed external document
    status = descriptor["db"].get("status")
    config = descriptor.get("configuration")
    providers = descriptor["db"].get("providers")
    if (
        not isinstance(status, dict)
        or not isinstance(config, dict)
        or not isinstance(providers, dict)
        or not all(isinstance(p, dict) for p in providers.values())
    ):
        raise ValueError("Missing or invalid Grype scanner/DB descriptor")
    required = (
        descriptor.get("version"),
        descriptor.get("timestamp"),
        status.get("schemaVersion"),
        status.get("built"),
        status.get("path"),
    )
    if (
        descriptor.get("name") != "grype"
        or not all(required)
        or status.get("valid") is not True
        or not isinstance(config.get("ignore"), list)
        or not isinstance(config.get("match"), dict)
        or not descriptor.get("db", {}).get("providers")
    ):
        raise ValueError("Missing or invalid Grype scanner/DB descriptor")

    def sanitize(value):
        if isinstance(value, dict):
            return {
                k: sanitize(v)
                for k, v in value.items()
                if k not in {"reason", "registry", "credentials", "password", "token"}
            }
        if isinstance(value, list):
            return [sanitize(v) for v in value]
        if isinstance(value, str) and "://" in value:
            parsed = urlsplit(value)
            return urlunsplit(
                (parsed.scheme, parsed.hostname or "", parsed.path, "", "")
            )
        return value

    db_hash = hashlib.sha256()
    with Path(status["path"]).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            db_hash.update(chunk)
    policy_fields = (
        "match",
        "search",
        "ignore",
        "exclude",
        "only-fixed",
        "only-notfixed",
        "ignore-wontfix",
        "show-suppressed",
        "include-matcher-suppressions",
        "match-upstream-kernel-headers",
        "by-cve",
        "fix-channel",
    )
    metadata = {
        "schema_version": 1,
        "scanner": {k: descriptor[k] for k in ("name", "version", "timestamp")},
        "database": {
            "schema_version": status["schemaVersion"],
            "built": status["built"],
            "valid": True,
            "sha256": db_hash.hexdigest(),
            "providers": {
                name: {
                    k: sanitize(v)
                    for k, v in provider.items()
                    if k in {"captured", "input"}
                }
                for name, provider in descriptor["db"]["providers"].items()
            },
        },
        "effective_matching_configuration": sanitize(
            {k: config[k] for k in policy_fields if k in config}
        ),
        "raw_scope": "Repository ignore/fix-state exclusions disabled; native Grype matching exclusions retained",
    }
    output.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
    return metadata


def scan(target, tool, output, root):
    image = target["image"]
    if image == "-":
        image = f"{tool}-scan-target:{target['name']}"
        build_args = {}
        for name, variable in target.get("build_arg_env", {}).items():
            value = os.environ.get(variable, "")
            if not re.fullmatch(
                r"[a-zA-Z0-9./:_-]+@sha256:[a-f0-9]{64}", value
            ) or value.endswith("sha256:" + "0" * 64):
                raise ValueError(
                    f"{target['name']} requires reviewed digest-pinned {variable}"
                )
            build_args[name] = value
        # Retain the exact base input in coverage provenance, including failures.
        target["build_args"] = build_args
        for value in build_args.values():
            authenticate_registry(value)
        prepare(target, root)
        authenticate_dockerfile(root / target["dockerfile"])
        options = [
            item
            for name, value in sorted(build_args.items())
            for item in ("--build-arg", f"{name}={value}")
        ]
        command(
            [
                "docker",
                "build",
                "--no-cache",
                "--pull",
                "--progress=plain",
                *options,
                "-f",
                target["dockerfile"],
                "-t",
                image,
                target["context"],
            ],
            # Cold runner images compile Terraform and Kaniko from source.
            # Keep a finite per-build bound inside the 150-minute project cap.
            timeout=1800,
            cwd=root,
        )
    else:
        authenticate_registry(image)
        command(["docker", "pull", "--platform", "linux/amd64", image], timeout=600)
    archive = output.with_suffix(".image.tar")
    catalog = output.with_suffix(".syft.json")
    try:
        inspected = command(
            ["docker", "image", "inspect", "--format", "{{.Id}}", image],
            stdout=subprocess.PIPE,
            text=True,
            timeout=30,
        )
        digest = inspected.stdout.strip()
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise ValueError("Docker did not report an immutable sha256 image ID")
        # Docker's streaming source can buffer large layers inside the scanner.
        # A seekable archive plus bounded Go/cataloger concurrency keeps the
        # full package scope while fitting the shared CodeBuild memory budget.
        command(["docker", "save", "--output", str(archive), image], timeout=600)
        scanner_env = {**os.environ, "GOMEMLIMIT": "2GiB", "SYFT_PARALLELISM": "2"}
        if tool == "grype":
            # Raw evidence bypasses configured ignore/fix-state exclusions and the
            # downstream filter. Grype-native matching exclusions still apply.
            raw_config = output.with_suffix(".raw-config.yaml")
            config = yaml.safe_load((root / ".grype.yaml").read_text()) or {}
            config["ignore"] = []
            config["only-fixed"] = False
            config["only-notfixed"] = False
            config["ignore-wontfix"] = ""
            raw_config.write_text(yaml.safe_dump(config))
            scan_env = {
                k: v
                for k, v in scanner_env.items()
                if not k.startswith("GRYPE_IGNORE")
                and k not in {"GRYPE_ONLY_FIXED", "GRYPE_ONLY_NOTFIXED"}
            }
            # Finish cataloging in its own process before matching. Keeping
            # both the expanded image and vulnerability matches in one Grype
            # process exceeded the 7 GiB CodeBuild host on SkyPilot.
            command(
                ["syft", "docker-archive:" + str(archive), "-o", "syft-json=" + str(catalog)],
                timeout=600,
                env=scanner_env,
            )
            descriptor_path = output.with_suffix(".descriptor.json")
            with output.open("w") as stream:
                command(
                    [
                        "grype",
                        "sbom:" + str(catalog),
                        "-o",
                        "sarif",
                        "-o",
                        "json=" + str(descriptor_path),
                        "--config",
                        str(raw_config),
                    ],
                    stdout=stream,
                    timeout=600,
                    env=scan_env,
                )
            try:
                scanner_metadata(
                    descriptor_path, output.with_suffix(".scanner-metadata.json")
                )
            finally:
                descriptor_path.unlink(missing_ok=True)
            raw_output = output.with_suffix(".raw.sarif")
            summary_output = output.with_suffix(".suppression-summary.json")
            command(
                [
                    "python3",
                    str(root / "codebuild/filter-sarif-ignores.py"),
                    "--sarif",
                    str(output),
                    "--config",
                    str(root / ".grype.yaml"),
                    "--output",
                    str(output),
                    "--raw-output",
                    str(raw_output),
                    "--summary-output",
                    str(summary_output),
                ]
            )
        else:
            command(
                ["syft", "docker-archive:" + str(archive), "-o", "cyclonedx-json=" + str(output)],
                timeout=600,
                env=scanner_env,
            )
        document = json.loads(output.read_text())
        if tool == "grype" and not document.get("runs"):
            raise ValueError("Scanner did not produce SARIF runs")
        if tool == "syft" and document.get("bomFormat") != "CycloneDX":
            raise ValueError("Scanner did not produce a CycloneDX SBOM")
        return digest
    finally:
        archive.unlink(missing_ok=True)
        catalog.unlink(missing_ok=True)
        subprocess.run(
            ["docker", "rmi", image],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tool", choices=("grype", "syft"))
    args = parser.parse_args()
    root = Path.cwd()
    scope = os.environ.get("SECURITY_IMAGE_SCOPE", "all")
    if scope not in {"all", "superplane"}:
        raise ValueError(f"Invalid image scope: {scope}")
    targets = discover(root, scope)
    date = os.environ.get("SECURITY_SCAN_DATE") or datetime.datetime.now(
        datetime.timezone.utc
    ).strftime("%Y/%m/%d")
    run_id = os.environ["CODEBUILD_BUILD_ID"].rsplit(":", 1)[-1]
    bucket = os.environ["SECURITY_SCANS_BUCKET"]
    prefix = (
        f"sarif/{date}/{run_id}/grype"
        if args.tool == "grype"
        else f"sbom/{date}/{run_id}"
    )
    evidence_prefix = os.environ.get("SECURITY_EVIDENCE_PREFIX", prefix).strip("/")
    report = {
        "tool": args.tool,
        "scope": scope,
        "commit": os.environ["ADP_SOURCE_SHA"],
        "expected": len(targets),
        "succeeded": 0,
        "targets": [],
    }
    print(json.dumps({"scope": scope, "targets": targets}, indent=2), flush=True)
    with tempfile.TemporaryDirectory(prefix="security-images-") as temp:
        for target in targets:
            result = dict(target, status="failed")
            suffix = ".sarif" if args.tool == "grype" else ".cdx.json"
            output = Path(temp) / (target["name"] + suffix)
            try:
                digest = scan(target, args.tool, output, root)
                uri = f"s3://{bucket}/{prefix}/{output.name}"
                command(
                    [
                        "aws",
                        "s3",
                        "cp",
                        str(output),
                        uri,
                        "--metadata",
                        f"commit={report['commit']}",
                    ]
                )
                artifact_sha256 = hashlib.sha256(output.read_bytes()).hexdigest()
                evidence_artifact_uri = (
                    f"s3://{bucket}/{evidence_prefix}/artifacts/{output.name}"
                )
                command(
                    [
                        "aws",
                        "s3",
                        "cp",
                        str(output),
                        evidence_artifact_uri,
                        "--metadata",
                        f"commit={report['commit']}",
                    ]
                )
                # Upload pre-filter (raw) and suppression summary for Grype.
                if args.tool == "grype":
                    raw_path = output.with_suffix(".raw.sarif")
                    summary_path = output.with_suffix(".suppression-summary.json")
                    if raw_path.exists():
                        raw_uri = (
                            f"s3://{bucket}/{evidence_prefix}/artifacts/{raw_path.name}"
                        )
                        command(
                            [
                                "aws",
                                "s3",
                                "cp",
                                str(raw_path),
                                raw_uri,
                                "--metadata",
                                f"commit={report['commit']}",
                            ]
                        )
                        result["raw_artifact"] = raw_uri
                        result["raw_artifact_sha256"] = hashlib.sha256(
                            raw_path.read_bytes()
                        ).hexdigest()
                    if summary_path.exists():
                        summary_uri = f"s3://{bucket}/{evidence_prefix}/artifacts/{summary_path.name}"
                        command(
                            [
                                "aws",
                                "s3",
                                "cp",
                                str(summary_path),
                                summary_uri,
                                "--metadata",
                                f"commit={report['commit']}",
                            ]
                        )
                        result["suppression_summary"] = summary_uri
                        result["suppression_summary_sha256"] = hashlib.sha256(
                            summary_path.read_bytes()
                        ).hexdigest()
                if args.tool == "grype":
                    metadata_path = output.with_suffix(".scanner-metadata.json")
                    metadata_bytes = metadata_path.read_bytes()
                    metadata_uri = f"s3://{bucket}/{evidence_prefix}/artifacts/{metadata_path.name}"
                    command(["aws", "s3", "cp", str(metadata_path), metadata_uri])
                    result["scanner_metadata"] = metadata_uri
                    result["scanner_metadata_sha256"] = hashlib.sha256(
                        metadata_bytes
                    ).hexdigest()
                provenance = Path(temp) / (target["name"] + ".provenance.json")
                provenance.write_text(
                    json.dumps(
                        {
                            "artifact_sha256": artifact_sha256,
                            "raw_artifact_sha256": result.get("raw_artifact_sha256"),
                            "suppression_summary_sha256": result.get(
                                "suppression_summary_sha256"
                            ),
                            "scanner_metadata_sha256": result.get(
                                "scanner_metadata_sha256"
                            ),
                            "build_args": target.get("build_args", {}),
                            "digest": digest,
                            "name": target["name"],
                            "source_revision": report["commit"],
                            "tool": args.tool,
                        },
                        sort_keys=True,
                    )
                    + "\n"
                )
                provenance_uri = (
                    f"s3://{bucket}/{evidence_prefix}/provenance/{target['name']}.json"
                )
                command(["aws", "s3", "cp", str(provenance), provenance_uri])
                result.update(
                    status="succeeded",
                    artifact=uri,
                    artifact_sha256=artifact_sha256,
                    evidence_artifact=evidence_artifact_uri,
                    digest=digest,
                    provenance=provenance_uri,
                )
                report["succeeded"] += 1
            except (subprocess.SubprocessError, OSError, ValueError) as exc:
                result["error"] = str(exc)
                print(f"ERROR: {target['name']}: {exc}", flush=True)
            result["build_args"] = target.get("build_args", {})
            report["targets"].append(result)
        coverage = Path(temp) / "coverage.json"
        coverage.write_text(json.dumps(report, indent=2) + "\n")
        command(
            [
                "aws",
                "s3",
                "cp",
                str(coverage),
                f"s3://{bucket}/{evidence_prefix}/coverage.json",
            ]
        )
    print(f"Coverage: {report['succeeded']}/{report['expected']} images", flush=True)
    # Every expected target must produce a real result. The previous rule passed
    # the build at >=50% coverage, which is how the 2026-09-21 run reported
    # success on 8/17 Grype and 6/17 Syft images: the images that failed to build
    # were skipped silently, so "no findings" was indistinguishable from "never
    # scanned". Name the missing targets and fail instead (#5618).
    missing = [
        item["name"] for item in report["targets"] if item["status"] != "succeeded"
    ]
    if missing:
        print(
            f"ERROR: incomplete coverage — {len(missing)}/{report['expected']} "
            f"target(s) produced no usable result: {', '.join(missing)}",
            flush=True,
        )
    return int(bool(missing))


if __name__ == "__main__":
    raise SystemExit(main())
