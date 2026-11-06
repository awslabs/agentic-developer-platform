"""Controller readback acceptance for the user-data Crew bootstrap (#8055).

A release in user-data mode launches the base AMI; the controller must still verify the
installed profile on the box and, additionally, the first-boot receipt
/run/adp/crew-bootstrap.json. The readback command is executed here for real against a
temporary layout (paths and the trusted uid substituted, as the enrollment tests do), so
the fail-closed cases are observed behaviour rather than string assertions.
"""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from controller.identity import (  # noqa: E402
    attest_release_host,
    crew_profile_command,
    crew_source_revision,
    host_profile_command,
    owner_home_command,
    ssh_host_key_command,
    workspace_mount_command,
    expected_image_id,
    generation_pins,
    host_refusal_class,
    parse_release_pins_readback,
    release_image_matches,
    release_pins_discovery_command,
)
from controller.launch import LaunchTemplate, ReleasePins, release_pins  # noqa: E402
from controller.worker import validate_release  # noqa: E402

PROFILE = "crew-r1"
BASE_SOURCE = "a" * 40
BUNDLE_SOURCE = "b" * 40
BUNDLE_SHA = "c" * 64
BUNDLE_URI = f"s3://adp-terraform-state-000000000000/dev-box/image-inputs/{BUNDLE_SHA}/crew.tar.gz"


def image_template(**overrides):
    values = dict(template_id="lt-example", version="4", region="us-east-1", image_id="ami-base", revision="r1",
                  profile_revision=PROFILE, source_revision=BASE_SOURCE)
    values.update(overrides)
    return LaunchTemplate(**values)


def user_data_template(**overrides):
    values = dict(crew_bootstrap="user-data", crew_bundle_sha256=BUNDLE_SHA, crew_source_revision=BUNDLE_SOURCE)
    values.update(overrides)
    return image_template(**values)


@pytest.fixture
def host(tmp_path):
    """An installed profile layout plus a receipt location, as the bootstrap leaves them."""
    prefix = tmp_path / "opt-adp-crew"
    unit_dir = tmp_path / "systemd"
    run_dir = tmp_path / "run-adp"
    revision = prefix / PROFILE
    for relative in ("venv/bin/kirocrew", "host/crew-host-dashboard.js", "host/crew-host-model.js", "host/crew-host-bootstrap.js"):
        (revision / relative).parent.mkdir(parents=True, exist_ok=True)
        (revision / relative).write_text("")
    (revision / "profile.json").write_text(json.dumps({"revision": PROFILE, "source_revision": BUNDLE_SOURCE}))
    (prefix / "current").symlink_to(PROFILE)
    unit_dir.mkdir()
    for unit in ("adp-crew.service", "adp-crew-host.service"):
        (unit_dir / unit).write_text("[Unit]\n")
    run_dir.mkdir()
    return SimpleNamespace(prefix=prefix, unit_dir=unit_dir, run_dir=run_dir, marker=run_dir / "crew-bootstrap.json")


def run_on_host(host, command):
    localized = (command.replace("/opt/adp/crew", str(host.prefix)).replace("/etc/systemd/system", str(host.unit_dir))
                 .replace("/run/adp", str(host.run_dir)).replace("st_uid==0", f"st_uid=={os.getuid()}"))
    return subprocess.run(["bash", "-c", localized], capture_output=True, text=True, check=False)


def write_receipt(host, **overrides):
    receipt = {"schema_version": 1, "crew_bootstrap": "user-data", "status": "ok", "profile_revision": PROFILE,
               "source_revision": BUNDLE_SOURCE, "bundle_sha256": BUNDLE_SHA}
    receipt.update(overrides)
    host.marker.write_text(json.dumps(receipt))
    host.marker.chmod(0o600)


def test_image_mode_command_is_unchanged_and_needs_no_receipt(host):
    template = image_template(source_revision=BUNDLE_SOURCE)
    assert host_profile_command(template) == crew_profile_command(PROFILE, BUNDLE_SOURCE)
    assert "crew-bootstrap.json" not in host_profile_command(template)
    result = run_on_host(host, host_profile_command(template))
    assert result.returncode == 0 and result.stdout == "crew-profile-ok\n", result.stderr


def test_user_data_mode_checks_the_bundle_source_not_the_image_source(host):
    template = user_data_template()
    assert crew_source_revision(template) == BUNDLE_SOURCE != template.source_revision
    command = host_profile_command(template)
    assert BUNDLE_SOURCE in command and BASE_SOURCE not in command and BUNDLE_SHA in command
    assert "/run/adp/crew-bootstrap.json" in command
    write_receipt(host)
    result = run_on_host(host, command)
    assert result.returncode == 0 and result.stdout == "crew-profile-ok\n", result.stderr


@pytest.mark.parametrize(
    "receipt",
    [
        None,
        {"status": "failed"},
        {"status": "running"},
        {"bundle_sha256": "d" * 64},
        {"profile_revision": "crew-r2"},
        {"source_revision": BASE_SOURCE},
        {"crew_bootstrap": "image"},
    ],
    ids=["missing", "failed", "running", "other-bundle", "other-profile", "image-source", "image-mode-receipt"],
)
def test_user_data_mode_fails_closed_without_a_matching_ok_receipt(host, receipt):
    if receipt is not None:
        write_receipt(host, **receipt)
    result = run_on_host(host, host_profile_command(user_data_template()))
    assert result.returncode != 0 and result.stdout == ""


def test_user_data_mode_rejects_a_writable_or_linked_receipt(host):
    write_receipt(host)
    host.marker.chmod(0o666)
    assert run_on_host(host, host_profile_command(user_data_template())).returncode != 0
    host.marker.chmod(0o600)
    assert run_on_host(host, host_profile_command(user_data_template())).returncode == 0
    linked = host.run_dir / "real.json"
    host.marker.rename(linked)
    host.marker.symlink_to(linked)
    assert run_on_host(host, host_profile_command(user_data_template())).returncode != 0


def test_user_data_mode_still_requires_the_installed_profile(host):
    write_receipt(host)
    (host.prefix / PROFILE / "host" / "crew-host-bootstrap.js").unlink()
    assert run_on_host(host, host_profile_command(user_data_template())).returncode != 0


def test_user_data_release_without_a_digest_cannot_pass(host):
    write_receipt(host)
    template = user_data_template(crew_bundle_sha256=None)
    assert host_profile_command(template) == "false"
    assert run_on_host(host, host_profile_command(template)).returncode != 0


@pytest.mark.asyncio
async def test_attestation_refuses_a_user_data_release_without_a_digest_before_any_provider_call():
    class Untouched:
        def __getattr__(self, name):
            raise AssertionError(f"provider call {name} must not happen")

    template = user_data_template(crew_bundle_sha256=None)
    assert await attest_release_host(Untouched(), Untouched(), None, None, None, template) is False


def manifest(**overrides):
    document = {
        "component": "dev-box", "deployment_id": "test-deployment", "account_id": "000000000000", "region": "us-east-1",
        "image_id": "ami-base", "template_revision": "r1", "instance_size": "t3.small", "workspace_disk_gb": 40,
        "source_revision": BASE_SOURCE, "kms_key_arn": "arn:aws:kms:us-east-1:000000000000:key/example",
        "run_table_arn": "arn:aws:dynamodb:us-east-1:000000000000:table/runs",
        "gateway_execute_arn": "arn:aws:execute-api:us-east-1:000000000000:api/dev", "crew_profile_revision": PROFILE,
        "crew_model_id": "example-model", "dashboard_enabled": True,
    }
    document.update({key: value for key, value in overrides.items() if value is not None})
    for key, value in overrides.items():
        if value is None:
            document.pop(key, None)
    document["manifest_sha256"] = hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return document


def outputs(**overrides):
    values = {"image_id": "ami-base", "source_revision": BASE_SOURCE, "template_revision": "r1", "launch_template_id": "lt-example",
              "launch_template_version": 4, "run_table_arn": "arn:aws:dynamodb:us-east-1:000000000000:table/runs"}
    values.update(overrides)
    return {key: {"value": value} for key, value in values.items()}


def test_validate_release_defaults_to_image_mode_without_bootstrap_fields():
    template = validate_release(manifest(), outputs(), "test-deployment")
    assert template.crew_bootstrap == "image" and template.crew_bundle_sha256 is None and template.crew_source_revision is None


def test_validate_release_carries_user_data_pins_when_the_foundation_agrees():
    template = validate_release(
        manifest(crew_bootstrap="user-data", crew_bundle_s3_uri=BUNDLE_URI, crew_bundle_sha256=BUNDLE_SHA, crew_source_revision=BUNDLE_SOURCE),
        outputs(crew_bootstrap="user-data"), "test-deployment",
    )
    assert template.crew_bootstrap == "user-data" and template.crew_bundle_sha256 == BUNDLE_SHA
    assert template.crew_source_revision == BUNDLE_SOURCE and template.source_revision == BASE_SOURCE


@pytest.mark.parametrize(
    "manifest_changes,output_changes,message",
    [
        ({"crew_bootstrap": "user-data", "crew_bundle_sha256": BUNDLE_SHA, "crew_source_revision": BUNDLE_SOURCE}, {}, "differs from deployed infrastructure"),
        ({}, {"crew_bootstrap": "user-data"}, "differs from deployed infrastructure"),
        ({"crew_bootstrap": "user-data", "crew_source_revision": BUNDLE_SOURCE}, {"crew_bootstrap": "user-data"}, "user-data bootstrap is not pinned"),
        ({"crew_bootstrap": "user-data", "crew_bundle_sha256": BUNDLE_SHA}, {"crew_bootstrap": "user-data"}, "user-data bootstrap is not pinned"),
        ({"crew_bootstrap": "user-data", "crew_bundle_sha256": BUNDLE_SHA, "crew_source_revision": BUNDLE_SOURCE, "crew_profile_revision": None,
          "gateway_execute_arn": None, "crew_model_id": None, "dashboard_enabled": False}, {"crew_bootstrap": "user-data"}, "user-data bootstrap is not pinned"),
        ({"crew_bootstrap": "ssm"}, {"crew_bootstrap": "ssm"}, "bootstrap mode is unknown"),
    ],
    ids=["foundation-still-image", "manifest-still-image", "no-digest", "no-source", "no-profile", "unknown-mode"],
)
def test_validate_release_fails_closed_on_unpinned_or_disagreeing_bootstrap(manifest_changes, output_changes, message):
    with pytest.raises(ValueError, match=message):
        validate_release(manifest(**manifest_changes), outputs(**output_changes), "test-deployment")


def test_workspace_mount_command_addresses_the_disk_by_volume_id_and_never_formats_a_used_disk():
    """The owner's retained workspace disk is the Crew home (#8223: it sat blank and unmounted on dev).
    The command names the device by EBS volume id, formats only a disk with no filesystem, mounts
    idempotently, records fstab with nofail and prints nothing to stdout."""
    command = workspace_mount_command("vol-09b67859b1a446c98")
    assert subprocess.run(["bash", "-n", "-c", command], capture_output=True).returncode == 0
    assert "/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_vol09b67859b1a446c98" in command
    assert "/dev/xvdf" not in command and "nvme1n1" not in command
    # format guard: only when blkid reports no TYPE at all; an xfs disk is reused untouched
    assert 'blkid -o value -s TYPE "$WS"' in command and "mkfs.xfs -q -L adp-crew" in command
    assert command.index("= xfs ]") < command.index("mkfs.xfs")
    assert "mountpoint -q /mnt/crew || mount -t xfs -o noatime,nodev,nosuid" in command
    assert "LABEL=adp-crew /mnt/crew xfs noatime,nodev,nosuid,nofail 0 2" in command
    # #8835 disk growth: the filesystem follows an enlarged volume on every boot; unchanged disks are a no-op
    assert "{ xfs_growfs -d /mnt/crew >/dev/null 2>&1 || true; }" in command
    assert command.index("mount -t xfs") < command.index("xfs_growfs") < command.index("chown root:root")
    assert command.endswith('{ [ -n "$(ls -A /mnt/crew | grep -vx .adp-host)" ] || chown root:root /mnt/crew; }'), (
        "an EMPTY home goes back to root for the restore helper's trust check; content is never touched; "
        "the host's own .adp-host state is not content"
    )
    assert "stat -c" not in command, "after a restore the home belongs to agent; readiness must not insist on root"
    assert "echo" not in command and "printf 'crew" not in command


def test_ssh_host_key_command_keeps_one_host_identity_on_the_retained_disk():
    """A Replace must not look like a man-in-the-middle to the owner's ssh (dev 2026-10-26): stored keys are
    installed (root:ssh_keys 0640, label restored) and sshd restarts only when they differ; a first
    generation stores its own keys; the chain never fails on it and nothing is printed."""
    command = ssh_host_key_command()
    assert subprocess.run(["bash", "-n", "-c", command], capture_output=True).returncode == 0
    assert "install -d -m 0700 -o root -g root /mnt/crew/.adp-host" in command
    assert "install -d -m 0700 -o root -g root /mnt/crew/.adp-host/ssh-host-keys" in command
    # dev gen 55 stored the keys at the Crew home root; they move under .adp-host once
    assert "[ ! -d /mnt/crew/.adp-ssh-host-keys ] || [ -d /mnt/crew/.adp-host/ssh-host-keys ] || mv /mnt/crew/.adp-ssh-host-keys /mnt/crew/.adp-host/ssh-host-keys" in command
    assert 'cmp -s "$key" /etc/ssh/$name' in command and "systemctl restart sshd" in command
    assert command.index("cmp -s") < command.index("chown root:ssh_keys /etc/ssh/ssh_host_*_key") < command.index("restorecon -F /etc/ssh/ssh_host_*") < command.index("systemctl restart sshd")
    assert "else cp -p /etc/ssh/ssh_host_*_key /etc/ssh/ssh_host_*_key.pub /mnt/crew/.adp-host/ssh-host-keys/" in command
    assert command.endswith(">/dev/null 2>&1 || true")


def test_owner_home_command_moves_the_developer_onto_the_retained_disk_with_a_shared_workspace():
    """Developers SSH in and run Claude Code or Codex under tmux (owner, 2026-10-26): their home must survive a
    Replace and the 500 GB disk must be theirs too. The home is seeded once from the image, bind-mounted over
    /home/ec2-user with fstab (nofail); /workspace is shared with the Crew agent through the `crew` group; all
    of it lives under the host's .adp-host state so checkpoints skip it and a fresh disk still reads empty."""
    command = owner_home_command()
    assert subprocess.run(["bash", "-n", "-c", command], capture_output=True).returncode == 0
    assert "install -d -m 0700 -o root -g root /mnt/crew/.adp-host /mnt/crew/.adp-host/home" in command
    assert "getent group crew >/dev/null || groupadd -r crew" in command
    assert "usermod -aG crew ec2-user && usermod -aG crew agent" in command
    seed = "[ -d /mnt/crew/.adp-host/home/ec2-user ] || { install -d -m 0700 -o ec2-user -g ec2-user /mnt/crew/.adp-host/home/ec2-user && cp -a /home/ec2-user/. /mnt/crew/.adp-host/home/ec2-user/; }"
    assert seed in command, "the image skeleton seeds the home exactly once; afterwards it is the owner's"
    assert "mountpoint -q /home/ec2-user || mount --bind /mnt/crew/.adp-host/home/ec2-user /home/ec2-user" in command
    assert "'/mnt/crew/.adp-host/home/ec2-user /home/ec2-user none bind,nofail 0 0' >> /etc/fstab" in command
    assert "install -d -m 2775 -o ec2-user -g crew /mnt/crew/.adp-host/workspace && install -d -m 0755 /workspace" in command
    assert "mountpoint -q /workspace || mount --bind /mnt/crew/.adp-host/workspace /workspace" in command
    assert "'/mnt/crew/.adp-host/workspace /workspace none bind,nofail 0 0' >> /etc/fstab" in command
    assert command.index("usermod") < command.index("cp -a") < command.index("mount --bind /mnt/crew/.adp-host/home") < command.index("mount --bind /mnt/crew/.adp-host/workspace") < command.index("restorecon -R /home/ec2-user /workspace")
    assert command.endswith(">/dev/null 2>&1 || true")


@pytest.mark.parametrize("volume_id", ["", None, "vol-XYZ", "vol-09b67859b1a446c98; rm -rf /", "i-0123456789abcdef0"])
def test_workspace_mount_command_refuses_anything_but_a_volume_id(volume_id):
    assert workspace_mount_command(volume_id) == "false"


# ── #8223 cause 34: a generation is verified against the release it was provisioned from ──


def test_a_profile_mismatch_names_itself_on_stderr(host):
    """The host records the release it was installed from; a controller pinning a newer one must
    be told so by name, not by a bare exit status (dev 2026-10-21: an hour of "lease fenced")."""
    write_receipt(host)
    result = run_on_host(host, host_profile_command(user_data_template(profile_revision="crew-r2")))
    assert result.returncode != 0 and result.stdout == ""
    assert f"Crew host profile refused: CrewProfileMismatch: expected=crew-r2 got={PROFILE}" in result.stderr
    assert host_refusal_class(result.stderr, "Failed") == "host_refused:CrewProfileMismatch"


def test_a_missing_receipt_names_itself(host):
    result = run_on_host(host, host_profile_command(user_data_template()))
    assert result.returncode != 0
    assert "Crew host profile refused: CrewReceiptMissing:" in result.stderr
    assert host_refusal_class(result.stderr, "Failed") == "host_refused:CrewReceiptMissing"


def test_an_installed_profile_that_differs_from_the_receipt_names_itself(host):
    write_receipt(host, profile_revision="crew-r2")
    result = run_on_host(host, host_profile_command(user_data_template(profile_revision="crew-r2")))
    assert result.returncode != 0
    assert f"Crew host profile refused: CrewProfileMismatch: expected=crew-r2 got={PROFILE}" in result.stderr


def test_generation_pins_prefer_the_instance_row_and_fall_back_to_the_release():
    current = user_data_template(profile_revision="crew-r9", crew_bundle_sha256="d" * 64, crew_source_revision="e" * 40,
                                 image_id="ami-newer", version="11")
    row = SimpleNamespace(profile_revision=PROFILE, source_revision=BUNDLE_SOURCE, crew_bundle_sha256=BUNDLE_SHA,
                          image_ref="ami-base", launch_template_version="4")
    pinned = generation_pins(row, current)
    assert pinned == ReleasePins(profile_revision=PROFILE, source_revision=BUNDLE_SOURCE, crew_bundle_sha256=BUNDLE_SHA,
                                 crew_bootstrap="user-data", image_id="ami-base", launch_template_version="4")
    # The readback the controller sends is the one for the GENERATION's release, byte for byte.
    assert host_profile_command(pinned) == crew_profile_command(PROFILE, BUNDLE_SOURCE, bundle_sha256=BUNDLE_SHA)
    assert expected_image_id(row, current) == "ami-base"
    legacy = SimpleNamespace(profile_revision=None, source_revision=None, crew_bundle_sha256=None, image_ref="ami-base",
                             launch_template_version=None)
    assert generation_pins(legacy, current) == release_pins(current)
    assert expected_image_id(legacy, current) == "ami-newer"
    # An image-mode row records no digest and verifies without a receipt.
    image_row = SimpleNamespace(profile_revision=PROFILE, source_revision=BASE_SOURCE, crew_bundle_sha256=None,
                                image_ref="ami-base", launch_template_version="4")
    assert generation_pins(image_row, current).crew_bootstrap == "image"
    assert "crew-bootstrap.json" not in host_profile_command(generation_pins(image_row, current))


def test_release_image_matches_judges_a_pinned_generation_by_its_own_release():
    box = SimpleNamespace(profile="crew", template_revision="r1")
    current = user_data_template(image_id="ami-newer")
    pinned = SimpleNamespace(profile_revision=PROFILE, image_ref="ami-base")
    legacy = SimpleNamespace(profile_revision=None, image_ref="ami-base")
    assert release_image_matches(box, pinned, current)
    assert not release_image_matches(box, legacy, current)
    assert release_image_matches(box, legacy, user_data_template(image_id="ami-base"))
    assert not release_image_matches(SimpleNamespace(profile="crew", template_revision="r2"), pinned, current)


def test_the_discovery_readback_prints_the_receipt_pins_only_when_the_installed_profile_agrees(host):
    write_receipt(host)
    result = run_on_host(host, release_pins_discovery_command())
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"crew-profile-pins {PROFILE} {BUNDLE_SOURCE} {BUNDLE_SHA}\n"
    assert parse_release_pins_readback(result.stdout) == (PROFILE, BUNDLE_SOURCE, BUNDLE_SHA)
    write_receipt(host, profile_revision="crew-r2")
    assert run_on_host(host, release_pins_discovery_command()).returncode != 0
    host.marker.unlink()
    assert run_on_host(host, release_pins_discovery_command()).returncode != 0


@pytest.mark.parametrize("output", [
    "", "crew-profile-ok\n", "crew-profile-pins a b\n", f"crew-profile-pins {PROFILE} {BUNDLE_SOURCE} {BUNDLE_SHA}\nextra\n",
    f"crew-profile-pins ../x {BUNDLE_SOURCE} {BUNDLE_SHA}\n", f"crew-profile-pins {PROFILE} {BUNDLE_SOURCE} {'z' * 64}\n",
])
def test_malformed_discovery_readbacks_are_rejected(output):
    assert parse_release_pins_readback(output) is None
