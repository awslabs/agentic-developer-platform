"""Packages the checked-in remote scripts and ships them to the instance.

This is the boundary the review asked for. Previously the harness sent an SSM
command that ran `/home/ec2-user/adp-eval/worker.py`, but nothing created that
file: cloud-init made the directory and stopped. Every remote path therefore
invoked a script that did not exist, which no amount of merging or fixture
provisioning could have fixed.

The flow reuses the mechanism the pinned #5173 harness already runs in this exact
private subnet (`tests/e2e/tenant_validation/runner.py`): tar.gz to S3, then
`aws s3 cp` on the instance, `sha256sum -c`, extract, run as ec2-user. Not
chunked base64 over SSM — the instance role already grants `s3:GetObject` for the
harness's own bundle, so S3 needs nothing new, and a proven transfer path is
worth more here than a novel one.

    remote/*.py + personal_aws_worker.py   (checked in, reviewed)
        -> archive()          : one tar.gz, deterministic bytes
        -> upload()           : S3, SSE, private
        -> install_commands() : aws s3 cp, VERIFY, extract, --self-check
        -> dispatcher.py on the instance runs one purpose and prints JSON

Two properties matter more than the mechanics:

**The shipped bytes are the reviewed bytes.** `digest()` is computed here from the
checked-in files and asserted on the instance after download. A truncated object,
a stale leftover tree or a substituted archive fails at install time rather than
producing a confusing failure inside a journey.

**A purpose with no script cannot silently no-op.** `purposes()` comes from the
dispatcher's own registry, which is imported from the shipped source, so
`require_purpose()` names the module a developer must write. That is what
replaces the resolver defaulting to `None` — the failure mode where nine cases
had no implementation and the mapping still looked wired.
"""

from __future__ import annotations

import gzip
import hashlib
import importlib.util
import io
import shlex
import tarfile
from pathlib import Path

# Where the bundle lands on the instance. 0700, owned by ec2-user: the scripts
# read fixture material, so they must not be world-readable.
REMOTE_DIR = "/home/ec2-user/adp-eval"
BUNDLE_PATH = REMOTE_DIR + "/bundle.tar.gz"
DISPATCHER = REMOTE_DIR + "/remote/dispatcher.py"

PACKAGE_DIR = Path(__file__).resolve().parent
LOCAL_REMOTE_DIR = PACKAGE_DIR / "remote"

# The reviewed E04/E05 journey lives in the package, not in remote/, because it
# predates this directory and is imported by remote/personal_aws.py. It ships
# flat alongside the remote modules so that import resolves on the instance.
EXTRA_SOURCES = ("personal_aws_worker.py",)


class BundleError(RuntimeError):
    """The bundle could not be built or verified."""


def sources():
    """The checked-in scripts, sorted, as (arcname, bytes).

    Sorted and read from disk so the archive is a pure function of the tree: two
    runs of the same commit produce the same digest, which is what makes the
    on-instance verification meaningful.
    """
    if not LOCAL_REMOTE_DIR.is_dir():
        raise BundleError(f"No remote script directory at {LOCAL_REMOTE_DIR}")
    found = []
    for path in sorted(LOCAL_REMOTE_DIR.glob("*.py")):
        if path.is_file():
            found.append((f"remote/{path.name}", path.read_bytes()))
    if not found:
        raise BundleError(f"{LOCAL_REMOTE_DIR} contains no scripts to ship")
    names = {name for name, _ in found}
    if "remote/dispatcher.py" not in names:
        raise BundleError("The bundle has no dispatcher.py; nothing could be invoked")
    for extra in EXTRA_SOURCES:
        path = PACKAGE_DIR / extra
        if not path.is_file():
            raise BundleError(
                f"{extra} is referenced by the bundle but is not in the tree"
            )
        found.append((f"remote/{extra}", path.read_bytes()))
    return sorted(found)


def purposes():
    """Purposes the shipped code can actually execute.

    Read from the dispatcher's PURPOSES registry via the SAME file that gets
    shipped, loaded by path so importing it here cannot pick up a different copy.
    Deriving this from the shipped source rather than a list maintained beside it
    is what makes "the orchestrator asked for X" and "the instance can run X" the
    same statement.
    """
    path = LOCAL_REMOTE_DIR / "dispatcher.py"
    spec = importlib.util.spec_from_file_location("_cli_uplift_dispatcher", path)
    if spec is None or spec.loader is None:
        raise BundleError(f"Could not load the purpose registry from {path}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 - any failure here is a bundle defect
        raise BundleError(
            f"remote/dispatcher.py does not import cleanly: {type(exc).__name__}"
        ) from None
    registry = getattr(module, "PURPOSES", None)
    if not isinstance(registry, dict) or not registry:
        raise BundleError("remote/dispatcher.py declares no PURPOSES registry")
    shipped = {name for name, _ in sources()}
    unbacked = sorted(
        purpose
        for purpose, (module_name, _defaults) in registry.items()
        if f"remote/{module_name}.py" not in shipped
    )
    if unbacked:
        # A registry entry with no file is the exact defect this module exists to
        # prevent: it would resolve, then fail on the instance.
        raise BundleError(
            "The purpose registry names modules that are not in the tree: "
            + ", ".join(unbacked)
        )
    return tuple(sorted(registry))


def archive():
    """Deterministic tar.gz of the shipped scripts.

    Every varying field is pinned: per-entry mtime/uid/gid/uname in the tar, and
    the gzip header's own timestamp. Gzip is applied separately because
    `tarfile.open(mode="w:gz")` gives no way to fix that header timestamp, so a
    single-step archive would produce a different digest every second and the
    on-instance verification would be unassertable.
    """
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.GNU_FORMAT) as tar:
        for name, payload in sources():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mtime = 0
            info.mode = 0o600
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(payload))
    return gzip.compress(raw.getvalue(), compresslevel=9, mtime=0)


def digest(data=None):
    """SHA-256 the instance must reproduce before anything is executed."""
    return hashlib.sha256(archive() if data is None else data).hexdigest()


def file_hashes():
    """Per-script hashes, published as evidence of exactly what ran remotely."""
    return {name: hashlib.sha256(payload).hexdigest() for name, payload in sources()}


def object_key(evaluation_id):
    """Per-run S3 key, so two runs cannot read each other's bundle."""
    if not evaluation_id:
        raise BundleError("A bundle upload needs the evaluation ID for its key")
    return f"cli-uplift-eval/{evaluation_id}/bundle.tar.gz"


def upload(aws, bucket, evaluation_id, *, kms_key_id=None, data=None):
    """Put the archive in S3, encrypted and private. Returns (key, digest).

    Mirrors the pinned harness's own upload: server-side encryption always, and
    KMS when the run supplies a key. The bundle is code rather than credentials,
    but it is written to a bucket that also holds run state, so it inherits the
    same protection rather than relying on the bucket default.
    """
    payload = archive() if data is None else data
    key = object_key(evaluation_id)
    extra = (
        {"ServerSideEncryption": "aws:kms", "SSEKMSKeyId": kms_key_id}
        if kms_key_id
        else {"ServerSideEncryption": "AES256"}
    )
    aws.call(
        "s3",
        "put_object",
        Bucket=bucket,
        Key=key,
        Body=payload,
        ContentType="application/gzip",
        **extra,
    )
    return key, digest(payload)


def install_commands(bucket, key, expected_digest, *, region=None):
    """Shell that downloads, verifies and extracts the bundle.

    Verification is `sha256sum -c`, so a truncated or substituted object fails
    here rather than surfacing as a mystery ImportError inside a journey. The old
    tree is removed first: a leftover script from a previous attempt could
    otherwise be executed while the digest of what we just shipped still matched.

    The final `--self-check` imports every registered module as ec2-user. That is
    what turns "we shipped a bundle" into "the instance can run these purposes",
    and it happens before any stage depends on one.
    """
    if not (bucket and key and expected_digest):
        raise BundleError("install_commands needs a bucket, a key and a digest")
    source = f"s3://{bucket}/{key}"
    return [
        "set -eu",
        "umask 077",
        # SSM can register before cloud-init installs jq and arms the TTL.
        "cloud-init status --wait >/dev/null",
        f"install -d -o ec2-user -g ec2-user -m 700 {shlex.quote(REMOTE_DIR)}",
        f"rm -rf {shlex.quote(REMOTE_DIR + '/remote')} {shlex.quote(BUNDLE_PATH)}",
        "aws s3 cp "
        + shlex.quote(source)
        + " "
        + shlex.quote(BUNDLE_PATH)
        + (f" --region {shlex.quote(region)}" if region else ""),
        f"printf '%s  %s\\n' {shlex.quote(expected_digest)} {shlex.quote(BUNDLE_PATH)} | sha256sum -c -",
        f"tar -xzf {shlex.quote(BUNDLE_PATH)} -C {shlex.quote(REMOTE_DIR)}",
        f"chown -R ec2-user:ec2-user {shlex.quote(REMOTE_DIR)}",
        f"test -f {shlex.quote(DISPATCHER)}",
        "runuser -l ec2-user -c "
        + shlex.quote("python3 " + DISPATCHER + " --self-check"),
    ]


def manifest():
    """What the report publishes about the code that ran on the instance."""
    return {
        "digest": digest(),
        "purposes": list(purposes()),
        "files": file_hashes(),
    }


def require_purpose(purpose):
    """Fail loudly when a stage asks for a purpose the bundle cannot run.

    This is the replacement for a resolver that returned None. An absent
    implementation must name the file a developer has to write, not degrade into
    a stage that quietly did nothing.
    """
    available = purposes()
    if purpose not in available:
        raise BundleError(
            f"No remote script implements {purpose!r}; it must be registered in "
            "tests/e2e/cli_uplift/remote/dispatcher.py and backed by a module "
            "there. Implemented: " + ", ".join(available)
        )
    return purpose


__all__ = [
    "BUNDLE_PATH",
    "DISPATCHER",
    "REMOTE_DIR",
    "BundleError",
    "archive",
    "digest",
    "file_hashes",
    "install_commands",
    "manifest",
    "object_key",
    "purposes",
    "require_purpose",
    "sources",
    "upload",
]
