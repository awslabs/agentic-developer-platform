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
                raise ValueError(f"Missing preparation source for {target['name']}: {step[1]}")
            shutil.rmtree(destination, ignore_errors=True)
            shutil.copytree(source, destination)
        elif step[0] == "run" and len(step) > 1:
            command(step[1:], cwd=root)
        else:
            raise ValueError(f"Invalid preparation step for {target['name']}: {step}")


def scan(target, tool, output, root):
    image = target["image"]
    if image == "-":
        image = f"{tool}-scan-target:{target['name']}"
        prepare(target, root)
        command(["docker", "build", "--no-cache", "--pull", "--quiet",
                 "-f", target["dockerfile"], "-t", image, target["context"]], timeout=600)
    else:
        command(["docker", "pull", "--platform", "linux/amd64", image], timeout=600)
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
        if tool == "grype":
            with output.open("w") as stream:
                command(["grype", "docker:" + image, "-o", "sarif", "--config",
                         str(root / ".grype.yaml")], stdout=stream, timeout=600)
            command(["python3", str(root / "codebuild/filter-sarif-ignores.py"),
                     "--sarif", str(output), "--config", str(root / ".grype.yaml"),
                     "--output", str(output)])
        else:
            command(["syft", "docker:" + image, "-o", "cyclonedx-json=" + str(output)], timeout=600)
        document = json.loads(output.read_text())
        if tool == "grype" and not document.get("runs"):
            raise ValueError("Scanner did not produce SARIF runs")
        if tool == "syft" and document.get("bomFormat") != "CycloneDX":
            raise ValueError("Scanner did not produce a CycloneDX SBOM")
        return digest
    finally:
        subprocess.run(["docker", "rmi", image], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tool", choices=("grype", "syft"))
    args = parser.parse_args()
    root = Path.cwd()
    scope = os.environ.get("SECURITY_IMAGE_SCOPE", "all")
    if scope not in {"all", "superplane"}:
        raise ValueError(f"Invalid image scope: {scope}")
    targets = discover(root, scope)
    date = os.environ.get("SECURITY_SCAN_DATE") or datetime.datetime.now(datetime.timezone.utc).strftime("%Y/%m/%d")
    run_id = os.environ["CODEBUILD_BUILD_ID"].rsplit(":", 1)[-1]
    bucket = os.environ["SECURITY_SCANS_BUCKET"]
    prefix = f"sarif/{date}/{run_id}/grype" if args.tool == "grype" else f"sbom/{date}/{run_id}"
    evidence_prefix = os.environ.get("SECURITY_EVIDENCE_PREFIX", prefix).strip("/")
    report = {"tool": args.tool, "scope": scope, "commit": os.environ["ADP_SOURCE_SHA"],
              "expected": len(targets), "succeeded": 0, "targets": []}
    print(json.dumps({"scope": scope, "targets": targets}, indent=2), flush=True)
    with tempfile.TemporaryDirectory(prefix="security-images-") as temp:
        for target in targets:
            result = dict(target, status="failed")
            suffix = ".sarif" if args.tool == "grype" else ".cdx.json"
            output = Path(temp) / (target["name"] + suffix)
            try:
                digest = scan(target, args.tool, output, root)
                uri = f"s3://{bucket}/{prefix}/{output.name}"
                command(["aws", "s3", "cp", str(output), uri, "--metadata",
                         f"commit={report['commit']}"])
                artifact_sha256 = hashlib.sha256(output.read_bytes()).hexdigest()
                evidence_artifact_uri = f"s3://{bucket}/{evidence_prefix}/artifacts/{output.name}"
                command(["aws", "s3", "cp", str(output), evidence_artifact_uri, "--metadata",
                         f"commit={report['commit']}"])
                provenance = Path(temp) / (target["name"] + ".provenance.json")
                provenance.write_text(json.dumps({
                    "artifact_sha256": artifact_sha256,
                    "digest": digest,
                    "name": target["name"],
                    "source_revision": report["commit"],
                    "tool": args.tool,
                }, sort_keys=True) + "\n")
                provenance_uri = f"s3://{bucket}/{evidence_prefix}/provenance/{target['name']}.json"
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
            report["targets"].append(result)
        coverage = Path(temp) / "coverage.json"
        coverage.write_text(json.dumps(report, indent=2) + "\n")
        command(["aws", "s3", "cp", str(coverage), f"s3://{bucket}/{evidence_prefix}/coverage.json"])
    print(f"Coverage: {report['succeeded']}/{report['expected']} images", flush=True)
    # Every expected target must produce a real result. The previous rule passed
    # the build at >=50% coverage, which is how the 2026-09-21 run reported
    # success on 8/17 Grype and 6/17 Syft images: the images that failed to build
    # were skipped silently, so "no findings" was indistinguishable from "never
    # scanned". Name the missing targets and fail instead (#5618).
    missing = [item["name"] for item in report["targets"] if item["status"] != "succeeded"]
    if missing:
        print(
            f"ERROR: incomplete coverage — {len(missing)}/{report['expected']} "
            f"target(s) produced no usable result: {', '.join(missing)}",
            flush=True,
        )
    return int(bool(missing))


if __name__ == "__main__":
    raise SystemExit(main())
