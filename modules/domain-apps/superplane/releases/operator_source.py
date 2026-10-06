"""Publish/retrieve private exact-history source; credentials never enter artifacts.

The consumer's trust anchor is the manifest digest independently obtained from
an authorized source workflow, not the identity strings inside that manifest.
This standalone standard-library script can bootstrap an operator with no GitHub
source credential. It never changes broker grants or skips installer checks.
"""

from __future__ import annotations

import argparse
import base64
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

REPOSITORY = "aws-e/adp"
WORKFLOW = (
    REPOSITORY + "/.github/workflows/superplane-operator-source.yml@refs/heads/main"
)
REF = "refs/heads/reviewed-main"
MAX_BUNDLE = 2 * 1024**3
MAX_MANIFEST = 65536
ROOT = Path.cwd()


class SourceRefused(Exception):
    pass


def require(value, message):
    if not value:
        raise SourceRefused(message)


def run(args, *, cwd=None):
    # Never echo subprocess output, command arguments or credential environment.
    try:
        environment = None
        if args[0] == "git":
            environment = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith("GIT_")
            }
            environment.update(
                GIT_NO_LAZY_FETCH="1",
                GIT_CONFIG_NOSYSTEM="1",
                GIT_CONFIG_GLOBAL="/dev/null",
            )
        result = subprocess.run(
            args, cwd=cwd, env=environment, capture_output=True, text=True, timeout=900
        )
    except (OSError, subprocess.TimeoutExpired):
        raise SourceRefused("Source tool unavailable or timed out") from None
    require(result.returncode == 0, "Source tool failed; no verified source claimed")
    return result.stdout.strip()


def git(root, *args):
    return run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(root), *args])


def sha256(path):
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def target(account, region, environment, source):
    require(re.fullmatch(r"[0-9]{12}", account), "Explicit account required")
    require(
        re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-[0-9]+", region), "Explicit region required"
    )
    require(
        re.fullmatch(r"[a-z][a-z0-9-]{0,19}", environment),
        "Bounded environment required",
    )
    require(re.fullmatch(r"[a-f0-9]{40}", source), "Exact source SHA required")
    return (
        f"adp-terraform-state-{account}",
        f"superplane/releases/operator-source/{environment}/{source}",
    )


def aws(region, *args):
    return json.loads(
        run(["aws", "--region", region, "--no-cli-pager", *args, "--output", "json"])
    )


def selected_identity(account, region, role_arn, role_id=None):
    require(
        re.fullmatch(
            r"arn:aws:iam::"
            + account
            + r":role/(?:[A-Za-z0-9+=,.@_-]+/)*[A-Za-z0-9+=,.@_-]{1,64}",
            role_arn,
        ),
        "Role must belong to selected account",
    )
    role = role_arn.rsplit("/", 1)[1]
    caller = aws(region, "sts", "get-caller-identity")
    prefix = f"arn:aws:sts::{account}:assumed-role/{role}/"
    arn = caller.get("Arn", "")
    require(
        caller.get("Account") == account
        and arn.startswith(prefix)
        and arn[len(prefix) :]
        and "/" not in arn[len(prefix) :],
        "Wrong source transport role or account",
    )
    if role_id is not None:
        require(
            re.fullmatch(r"AROA[A-Z0-9]{17}", role_id),
            "Independently resolved RoleId required",
        )
        observed = aws(region, "iam", "get-role", "--role-name", role).get("Role", {})
        require(
            observed.get("Arn") == role_arn
            and observed.get("RoleId") == role_id
            and caller.get("UserId") == role_id + ":" + arn[len(prefix) :],
            "Selected operator role identity changed",
        )
    return {"role_arn": role_arn, "session_arn": arn}


def bucket_contract(account, region, bucket):
    def read(operation):
        return aws(
            region,
            "s3api",
            operation,
            "--bucket",
            bucket,
            "--expected-bucket-owner",
            account,
        )

    require(
        read("get-bucket-versioning").get("Status") == "Enabled",
        "Source bucket versioning must be enabled",
    )
    public = read("get-public-access-block").get("PublicAccessBlockConfiguration", {})
    require(
        all(
            public.get(key) is True
            for key in (
                "BlockPublicAcls",
                "IgnorePublicAcls",
                "BlockPublicPolicy",
                "RestrictPublicBuckets",
            )
        ),
        "Source bucket must block public access",
    )
    ownership = (
        read("get-bucket-ownership-controls")
        .get("OwnershipControls", {})
        .get("Rules", [])
    )
    require(
        ownership == [{"ObjectOwnership": "BucketOwnerEnforced"}],
        "Source bucket must enforce owner control",
    )
    location = read("get-bucket-location").get("LocationConstraint") or "us-east-1"
    require(location == region, "Source bucket region differs")


def exact_checkout(root, source):
    require(
        git(root, "rev-parse", "HEAD") == source,
        "Checkout differs from reviewed source",
    )
    require(
        git(root, "rev-parse", "--is-shallow-repository") == "false",
        "Shallow source is not an operator release",
    )
    require(
        not git(root, "status", "--porcelain", "--untracked-files=normal"),
        "Operator source must be clean",
    )
    git(root, "fsck", "--full", "--strict", "--no-dangling")
    # Missing promised blobs must not silently satisfy a partial clone's fsck.
    objects = git(root, "rev-list", "--objects", "--missing=print", source)
    require(
        not any(line.startswith("?") for line in objects.splitlines()),
        "Source history is incomplete",
    )


def bundle_header(path, source):
    require(
        0 < path.stat().st_size <= MAX_BUNDLE, "Bundle exceeds source transport bound"
    )
    with path.open("rb") as stream:
        require(
            stream.readline(128) == b"# v2 git bundle\n", "Unsupported bundle format"
        )
        require(
            stream.readline(256) == f"{source} {REF}\n".encode(),
            "Bundle must contain only the exact reviewed source ref",
        )
        require(
            stream.readline(2) == b"\n",
            "Prerequisite, filtered or additional bundle refs are refused",
        )


def make_bundle(root, source, destination):
    exact_checkout(root, source)
    with tempfile.TemporaryDirectory(prefix="operator-source-objects-") as temporary:
        bare = Path(temporary) / "repository.git"
        run(["git", "init", "--bare", str(bare)])
        git(bare, "fetch", "--no-tags", str(root), source)
        git(bare, "update-ref", REF, source)
        git(bare, "bundle", "create", "--version=2", str(destination), REF)
    bundle_header(destination, source)


def verify_bundle(path, source, destination):
    bundle_header(path, source)
    require(not destination.exists(), "Source checkout destination already exists")
    with tempfile.TemporaryDirectory(prefix="operator-source-verify-") as temporary:
        bare = Path(temporary) / "verify.git"
        run(["git", "init", "--bare", str(bare)])
        git(bare, "bundle", "verify", str(path))
    run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "clone",
            "--no-checkout",
            str(path),
            str(destination),
        ]
    )
    git(destination, "checkout", "--detach", source)
    git(
        destination,
        "remote",
        "set-url",
        "origin",
        "https://github.com/" + REPOSITORY + ".git",
    )
    exact_checkout(destination, source)
    require(
        git(destination, "rev-parse", "refs/remotes/origin/reviewed-main") == source,
        "Source ref changed",
    )
    return git(destination, "rev-parse", "HEAD^{tree}")


def put(account, region, bucket, key, path):
    checksum = sha256(path)
    try:
        value = aws(
            region,
            "s3api",
            "put-object",
            "--bucket",
            bucket,
            "--expected-bucket-owner",
            account,
            "--key",
            key,
            "--body",
            str(path),
            "--if-none-match",
            "*",
            "--checksum-sha256",
            base64.b64encode(bytes.fromhex(checksum)).decode(),
        )
    except SourceRefused:
        # Includes a lost PUT reply: read back exactly matching immutable bytes,
        # never overwrite or assume that a failed response means no object exists.
        value = aws(
            region,
            "s3api",
            "head-object",
            "--bucket",
            bucket,
            "--expected-bucket-owner",
            account,
            "--key",
            key,
        )
        with tempfile.TemporaryDirectory(
            prefix="operator-source-reconcile-"
        ) as temporary:
            get(
                account,
                region,
                bucket,
                {
                    "key": key,
                    "version_id": value.get("VersionId"),
                    "sha256": checksum,
                    "bytes": path.stat().st_size,
                },
                Path(temporary) / "object",
                max(path.stat().st_size, 1),
            )
    require(
        isinstance(value.get("VersionId"), str)
        and value["VersionId"] not in ("", "null")
        and value.get("ServerSideEncryption") in ("AES256", "aws:kms"),
        "Source object did not receive encrypted immutable version evidence",
    )
    return {
        "key": key,
        "version_id": value["VersionId"],
        "sha256": checksum,
        "bytes": path.stat().st_size,
    }


def get(account, region, bucket, item, path, maximum):
    require(
        isinstance(item.get("version_id"), str)
        and 0 < len(item["version_id"]) <= 1024
        and item["version_id"] != "null",
        "Exact source object version required",
    )
    args = [
        "--bucket",
        bucket,
        "--expected-bucket-owner",
        account,
        "--key",
        item["key"],
        "--version-id",
        item["version_id"],
    ]
    head = aws(region, "s3api", "head-object", *args)
    require(
        type(head.get("ContentLength")) is int
        and 0 < head["ContentLength"] <= maximum
        and head.get("VersionId") == item["version_id"]
        and head.get("ServerSideEncryption") in ("AES256", "aws:kms"),
        "Source object size/version/encryption differs",
    )
    result = aws(region, "s3api", "get-object", *args, str(path))
    require(
        result.get("VersionId") == item["version_id"]
        and path.stat().st_size == head["ContentLength"]
        and sha256(path) == item["sha256"],
        "Source object integrity differs",
    )
    if "bytes" in item:
        require(path.stat().st_size == item["bytes"], "Source object length differs")


def publish(args):
    bucket, prefix = target(
        args.account, args.region, args.environment, args.source_sha
    )
    require(
        os.environ.get("GITHUB_REPOSITORY") == REPOSITORY
        and os.environ.get("GITHUB_REF") == "refs/heads/main"
        and os.environ.get("GITHUB_SHA") == args.source_sha
        and os.environ.get("GITHUB_WORKFLOW_REF") == WORKFLOW,
        "Source publication requires the reviewed repository workflow on main",
    )
    run_id, attempt = (
        os.environ.get("GITHUB_RUN_ID", ""),
        os.environ.get("GITHUB_RUN_ATTEMPT", ""),
    )
    require(
        re.fullmatch(r"[1-9][0-9]*", run_id) and re.fullmatch(r"[1-9][0-9]*", attempt),
        "Source workflow identity missing",
    )
    root = args.root.resolve()
    exact_checkout(root, args.source_sha)
    consumer = Path(__file__).resolve()
    require(
        consumer == root / "modules/domain-apps/superplane/releases/operator_source.py",
        "Source exporter must execute from its exact reviewed checkout",
    )
    repo = json.loads(run(["gh", "api", "repos/" + REPOSITORY]))
    require(
        type(repo.get("id")) is int
        and repo["id"] > 0
        and repo.get("full_name") == REPOSITORY,
        "Source repository identity differs",
    )
    main = json.loads(
        run(["gh", "api", "repos/" + REPOSITORY + "/git/ref/heads/main"])
    )["object"]["sha"]
    require(re.fullmatch(r"[0-9a-f]{40}", main), "Maintained main did not resolve")
    compare = json.loads(
        run(["gh", "api", f"repos/{REPOSITORY}/compare/{args.source_sha}...{main}"])
    )
    require(
        compare.get("status") in ("ahead", "identical")
        and compare.get("merge_base_commit", {}).get("sha") == args.source_sha,
        "Source is not in authenticated maintained main history",
    )
    producer = selected_identity(
        args.account,
        args.region,
        f"arn:aws:iam::{args.account}:role/adp-{args.environment}-trusted-build",
    )
    bucket_contract(args.account, args.region, bucket)
    require(
        not args.output.exists(),
        "Publication output already exists; reconcile any previous upload",
    )
    args.output.mkdir(parents=True, mode=0o700)
    bundle = args.output / "source.bundle"
    make_bundle(root, args.source_sha, bundle)
    bundle_item = put(
        args.account,
        args.region,
        bucket,
        prefix + "/bundles/" + sha256(bundle) + ".bundle",
        bundle,
    )
    consumer_item = put(
        args.account,
        args.region,
        bucket,
        prefix + "/consumers/" + sha256(consumer) + ".py",
        consumer,
    )
    manifest = {
        "version": 1,
        "repository": REPOSITORY,
        "repository_id": repo["id"],
        "account": args.account,
        "region": args.region,
        "environment": args.environment,
        "source_sha": args.source_sha,
        "source_tree": git(root, "rev-parse", "HEAD^{tree}"),
        "bundle": bundle_item,
        "consumer": consumer_item,
        "main_observation": {
            "main_sha": main,
            "status": compare["status"],
            "merge_base_sha": args.source_sha,
        },
        "producer": {
            **producer,
            "workflow": WORKFLOW,
            "run_id": run_id,
            "run_attempt": attempt,
        },
        "observed_at": datetime.now(UTC).isoformat(),
    }
    path = args.output / "manifest.json"
    path.write_bytes(canonical(manifest))
    receipt = put(
        args.account,
        args.region,
        bucket,
        prefix + "/manifests/" + sha256(path) + ".json",
        path,
    )
    receipt.update(
        bucket=bucket,
        account=args.account,
        region=args.region,
        environment=args.environment,
        source_sha=args.source_sha,
        consumer=consumer_item,
        status="operator-source-published",
        trust="Independently obtain this receipt from the authorized source workflow; embedded publisher strings are not authentication",
    )
    (args.output / "receipt.json").write_bytes(canonical(receipt))
    return receipt


def validate_manifest(manifest, args, prefix):
    require(
        isinstance(manifest, dict)
        and manifest.get("version") == 1
        and manifest.get("repository") == REPOSITORY
        and type(manifest.get("repository_id")) is int
        and manifest["repository_id"] > 0,
        "Unsupported source manifest",
    )
    for key, expected in (
        ("account", args.account),
        ("region", args.region),
        ("environment", args.environment),
        ("source_sha", args.source_sha),
    ):
        require(manifest.get(key) == expected, "Source manifest target differs")
    require(
        re.fullmatch(r"[0-9a-f]{40}", str(manifest.get("source_tree", ""))),
        "Source tree missing",
    )
    main = manifest.get("main_observation", {})
    require(
        isinstance(main, dict)
        and main.get("status") in ("ahead", "identical")
        and main.get("merge_base_sha") == args.source_sha
        and re.fullmatch(r"[0-9a-f]{40}", str(main.get("main_sha", ""))),
        "Authenticated main observation missing",
    )
    producer = manifest.get("producer", {})
    require(
        isinstance(producer, dict)
        and producer.get("workflow") == WORKFLOW
        and producer.get("role_arn")
        == f"arn:aws:iam::{args.account}:role/adp-{args.environment}-trusted-build",
        "Source publisher contract differs",
    )
    for field, directory, suffix, maximum in (
        ("bundle", "bundles", ".bundle", MAX_BUNDLE),
        ("consumer", "consumers", ".py", MAX_MANIFEST),
    ):
        item = manifest.get(field, {})
        require(
            isinstance(item, dict)
            and set(item) == {"key", "version_id", "sha256", "bytes"}
            and re.fullmatch(r"[a-f0-9]{64}", str(item.get("sha256", "")))
            and type(item.get("bytes")) is int
            and 0 < item["bytes"] <= maximum,
            "Malformed source object receipt",
        )
        require(
            item["key"] == prefix + "/" + directory + "/" + item["sha256"] + suffix,
            "Source object is outside the exact release prefix",
        )
    return manifest


def fetch(args):
    bucket, prefix = target(
        args.account, args.region, args.environment, args.source_sha
    )
    require(
        re.fullmatch(r"[0-9a-f]{64}", args.manifest_sha256),
        "Independently pinned manifest digest required",
    )
    selected_identity(args.account, args.region, args.role_arn, args.role_id)
    bucket_contract(args.account, args.region, bucket)
    require(
        not args.output.exists(),
        "Operator output already exists; never overwrite a checkout",
    )
    args.output.mkdir(parents=True, mode=0o700)
    manifest_path = args.output / "manifest.json"
    get(
        args.account,
        args.region,
        bucket,
        {
            "key": prefix + "/manifests/" + args.manifest_sha256 + ".json",
            "version_id": args.manifest_version_id,
            "sha256": args.manifest_sha256,
        },
        manifest_path,
        MAX_MANIFEST,
    )
    manifest = validate_manifest(json.loads(manifest_path.read_text()), args, prefix)
    require(
        sha256(Path(__file__).resolve()) == manifest["consumer"]["sha256"],
        "Consumer differs from independently pinned source manifest",
    )
    bundle = args.output / "source.bundle"
    selected_identity(args.account, args.region, args.role_arn, args.role_id)
    get(args.account, args.region, bucket, manifest["bundle"], bundle, MAX_BUNDLE)
    tree = verify_bundle(bundle, args.source_sha, args.output / "source")
    require(tree == manifest["source_tree"], "Verified source tree differs")
    receipt = {
        "status": "operator-source-verified",
        "repository": REPOSITORY,
        "source_sha": args.source_sha,
        "source_tree": tree,
        "manifest_sha256": args.manifest_sha256,
        "manifest_version_id": args.manifest_version_id,
        "checkout": str((args.output / "source").resolve()),
        "main_proof": "authorized publication snapshot; not a new live GitHub observation",
    }
    (args.output / "receipt.json").write_bytes(canonical(receipt))
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("publish", "fetch"))
    for name in ("account", "region", "environment", "source-sha"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--manifest-sha256", default="")
    parser.add_argument("--manifest-version-id", default="")
    parser.add_argument("--role-arn", default="")
    parser.add_argument("--role-id", default="")
    args = parser.parse_args()
    args.output = args.output.resolve()
    try:
        print(json.dumps(publish(args) if args.mode == "publish" else fetch(args)))
        return 0
    except SourceRefused as exc:
        print(json.dumps({"status": "refused", "reason": str(exc)}))
        return 2
    except Exception:
        print(
            json.dumps(
                {
                    "status": "refused",
                    "reason": "Source transport did not verify; retain private output and reconcile any publication before retrying",
                }
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
