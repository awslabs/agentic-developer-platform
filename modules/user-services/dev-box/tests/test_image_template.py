"""Offline checks for an explicitly admitted, immutable Dev Box image."""

import importlib.util
import re
from pathlib import Path

import pytest

IMAGE_DIR = Path(__file__).resolve().parents[1] / "image"
SPEC = importlib.util.spec_from_file_location(
    "dev_box_image_template", IMAGE_DIR / "render_template.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


@pytest.fixture
def inputs():
    template = {
        "name": "assistant",
        "profile": "crew",
        "revision": "r1",
        "approved": True,
        "instance_size": "t3.small",
        "disk_gb": 40,
        "image_ref": "ami-1234567890abcdef0",
    }
    policy = {"allowed_sizes": ["t3.small"], "max_disk_gb": 64}
    image = {
        "image_id": "ami-1234567890abcdef0",
        "image_revision": "base-v1",
        "source_revision": "a" * 40,
    }
    return template, policy, image


def test_renders_approved_versioned_template(inputs):
    template, policy, image = inputs
    assert MODULE.render_template(template, policy, image) == {
        "name": "assistant",
        "profile": "crew",
        "template_revision": "r1",
        "instance_size": "t3.small",
        "disk_gb": 40,
        **image,
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"approved": False},
        {"instance_size": "m7i.48xlarge"},
        {"disk_gb": 65},
        {"image_ref": "ami-11111111111111111"},
        {"image_ref": None},
        {"revision": "../../bad"},
    ],
)
def test_rejects_unapproved_or_unbounded_template(inputs, changes):
    template, policy, image = inputs
    template.update(changes)
    with pytest.raises(ValueError):
        MODULE.render_template(template, policy, image)


def test_base_image_is_pinned_private_encrypted_and_prepares_loopback_ssh():
    source = (IMAGE_DIR / "base.pkr.hcl").read_text()
    for required in (
        "source_ami",
        '"session_manager"',
        "associate_public_ip_address = false",
        "encrypt_boot",
        "kms_key_id",
        "source_revision",
        'post-processor "manifest"',
    ):
        assert required in source
    assert 'source      = "${path.root}/prepare_ssh.sh"' in source
    assert "sudo bash /tmp/adp-prepare-ssh.sh" in source
    assert "user_data" not in source


def test_image_refuses_missing_instance_connect_and_closes_guest_ssh():
    import subprocess

    script = IMAGE_DIR / "prepare_ssh.sh"
    assert subprocess.run(["bash", "-n", str(script)], check=False).returncode == 0
    source = script.read_text()
    for requirement in (
        "rpm -q openssh-server ec2-instance-connect amazon-ssm-agent",
        "test -x /opt/aws/bin/eic_run_authorized_keys",
        "ListenAddress 127.0.0.1",
        "PasswordAuthentication no",
        "PermitRootLogin no",
        "AllowUsers ec2-user",
        "ClientAliveInterval 300",
        "ClientAliveCountMax 2",
        "TMOUT=900",
        "grep -Eq '^authorizedkeyscommand /opt/aws/bin/eic_run_authorized_keys",
        "grep -Eq '^listenaddress 127\\.0\\.0\\.1",
    ):
        assert requirement in source


@pytest.mark.parametrize(
    "injected",
    [
        {"user_data": "owner token"},
        {"credentials": {"secret_access_key": "example"}},
        {"owner_workspace": "/home/owner"},
    ],
)
def test_rejects_secret_or_owner_data_in_image_metadata(inputs, injected):
    template, policy, image = inputs
    image.update(injected)
    with pytest.raises(ValueError, match="user data or credentials"):
        MODULE.render_template(template, policy, image)


def assert_no_broad_ec2_grants(source):
    without_denials = re.sub(
        r'\{\s*Effect\s*=\s*"Deny"\s+Action\s*=\s*\["ec2:\*"\]\s+'
        r'Resource\s*=\s*"\*"\s*\}',
        "",
        source,
    )
    assert '"ec2:*"' not in without_denials


@pytest.mark.parametrize(
    "statement",
    [
        '{ Effect = "Allow" Action = ["ec2:*"] Resource = "*" }',
        '{ Effect = "Deny" NotAction = ["ec2:*"] Resource = "*" }',
        '{ Effect = "Allow" Action = ["ec2:*"] Resource = "arn:aws:ec2:us-east-1:000000000000:instance/*" }',
    ],
)
def test_cloud_grant_guard_rejects_broad_ec2_actions_alongside_denials(statement):
    denial = '{ Effect = "Deny" Action = ["ec2:*"] Resource = "*" }'
    assert_no_broad_ec2_grants(denial)
    with pytest.raises(AssertionError):
        assert_no_broad_ec2_grants(statement)
    with pytest.raises(AssertionError):
        assert_no_broad_ec2_grants(f"Statement = [{denial}, {statement}]")


def test_guest_does_not_receive_broad_cloud_or_network_grants():
    infrastructure = (IMAGE_DIR.parent / "infra" / "main.tf").read_text()
    base_image = (IMAGE_DIR / "base.pkr.hcl").read_text()
    assert_no_broad_ec2_grants(infrastructure)
    assert '"ec2:*"' not in base_image
    for forbidden in ('"::/0"', '"bedrock:', '"ssm:*"'):
        assert forbidden not in infrastructure
        assert forbidden not in base_image
    assert '"0.0.0.0/0"' not in base_image
    # The one internet egress is HTTPS to the gateway's Regional REST API, present only
    # while a gateway is approved (#8223); any other 0.0.0.0/0, port or protocol is a
    # regression of the owner-box fence.
    assert infrastructure.count('"0.0.0.0/0"') == 1
    internet_rule = re.search(
        r'resource "aws_vpc_security_group_egress_rule" "api_gateway" \{\n(?P<body>(?:  [^\n]*\n)+)\}', infrastructure
    )
    assert internet_rule is not None
    body = internet_rule.group("body")
    assert '"0.0.0.0/0"' in body
    for line in (
        'count             = local.gateway_execute_arn != "" ? 1 : 0',
        'ip_protocol       = "tcp"',
        "from_port         = 443",
        "to_port           = 443",
    ):
        assert line in body, line
    # The only user data the guest can receive is the Crew bootstrap (#8055), rendered by
    # crew_bootstrap.tf and null in the default image mode (tests/crew-bootstrap.tftest.hcl).
    assert "user_data" not in base_image
    assert re.findall(r"user_data\b[^\n]*", infrastructure) == [
        "user_data = local.crew_bootstrap_user_data"
    ]
    assert "role       = aws_iam_role.guest.name" in infrastructure
    assert (
        'policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"'
        in infrastructure
    )


def assert_guest_has_no_ssh_ingress(source):
    assert re.findall(
        r'resource "aws_vpc_security_group_ingress_rule" "([^"]+)"', source
    ) == ["dashboard"]
    assert 'resource "aws_security_group_rule"' not in source
    guest = source.split('resource "aws_security_group" "guest" {', 1)[1].split(
        'resource "', 1
    )[0]
    assert not re.search(r"\bingress\s*\{", guest)
    dashboard = source.split(
        'resource "aws_vpc_security_group_ingress_rule" "dashboard" {', 1
    )[1].split('resource "', 1)[0]
    assert re.search(r"\bfrom_port\s*=\s*8443\b", dashboard)
    assert re.search(r"\bto_port\s*=\s*8443\b", dashboard)
    assert "referenced_security_group_id = var.gateway_security_group_id" in dashboard
    assert "count                        = local.dashboard_enabled ? 1 : 0" in dashboard


def test_guest_security_group_rejects_accidental_inbound_ssh():
    source = (IMAGE_DIR.parent / "infra" / "main.tf").read_text()
    assert_guest_has_no_ssh_ingress(source)
    with pytest.raises(AssertionError):
        assert_guest_has_no_ssh_ingress(
            source
            + '\nresource "aws_vpc_security_group_ingress_rule" "ssh" {\n  from_port = 22\n}\n'
        )
    with pytest.raises(AssertionError):
        assert_guest_has_no_ssh_ingress(
            source.replace(
                "from_port                    = 8443",
                "from_port                    = 22",
            )
        )


def test_crew_image_uses_only_pinned_local_profile_artifacts():
    source = (IMAGE_DIR / "crew.pkr.hcl").read_text()
    for required in (
        "source_ami",
        "source_revision",
        "image_revision",
        "profile_revision",
        "crew_bundle_s3_uri",
        "crew_bundle_sha256",
        "crew_manifest_path",
        '"${path.root}/install_crew.py"',
        # The ~1 GB bundle is fetched by the instance into disk-backed /var/tmp and
        # digest-checked before install, not streamed through the session tunnel.
        '"sudo aws s3 cp --region ${var.region} --only-show-errors ${var.crew_bundle_s3_uri} /var/tmp/adp-crew-inputs/crew.tar.gz"',
        "\"echo '${var.crew_bundle_sha256}  /var/tmp/adp-crew-inputs/crew.tar.gz' | sudo sha256sum -c --quiet -\"",
        "--bundle /var/tmp/adp-crew-inputs/crew.tar.gz --expected-source-revision",
        '"sudo rm -rf /var/tmp/adp-crew-inputs"',
        'regex("^s3://[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]/dev-box/image-inputs/[0-9a-f]{64}/crew\\\\.tar\\\\.gz$", var.crew_bundle_s3_uri)',
        '"${path.root}/write_enrollment.py"',
        '"${path.root}/verify_crew_ready.py"',
        "install -m 0700 -o root -g root /tmp/adp-write-enrollment.py",
        "install -m 0700 -o root -g root /tmp/adp-write-host-certificate.py",
        "install -m 0700 -o root -g root /tmp/adp-verify-crew-ready.py",
        "install -m 0700 -o root -g root /tmp/adp-restore-crew-host.py",
        "install -m 0700 -o root -g root /tmp/adp-checkpoint-crew-host.py",
        "install -m 0600 -o root -g root /tmp/adp-crew-checkpoint.py",
        '"${path.root}/checkpoint_crew_host.py"',
        "--expected-source-revision ${var.source_revision}",
        "--expected-profile-revision ${var.profile_revision}",
        '"adp:profile-revision" = var.profile_revision',
        '"adp:image-revision"   = var.image_revision',
        "associate_public_ip_address = false",
        "encrypt_boot                = true",
        'ssh_interface               = "session_manager"',
    ):
        assert required in source
    for forbidden in (
        "user_data",
        "api_key",
        "secret_access_key",
        "curl ",
        "wget ",
        "0.0.0.0/0",
        "bedrock:",
        "crew_bundle_path",
        "/tmp/adp-crew.tar.gz",
    ):
        assert forbidden not in source.lower()
    # Eight root-only helpers plus profile.json travel through the tunnel; the bundle does not.
    assert source.count('provisioner "file"') == 8
    assert "crew.tar.gz" not in "".join(
        re.findall(r'provisioner "file" \{[^}]*\}', source)
    )
    assert source.index("sha256sum -c --quiet -") < source.index(
        "/tmp/adp-install-crew.py --manifest"
    )
    # #8055: the installer runs on the bundle's CPython when present (AL2023's python3 is 3.9).
    assert '"$GUEST_PYTHON" /tmp/adp-install-crew.py --manifest' in source.replace(
        "\\", ""
    )
    assert source.index("adp-install-crew.py --manifest") < source.index(
        '"sudo rm -rf /var/tmp/adp-crew-inputs"'
    )
    assert 'post-processor "manifest"' in source


def test_packer_validation_messages_are_full_sentences():
    """Packer rejects a validation error_message that does not start uppercase and end with . or ? (seen live, #8054)."""
    import re
    from pathlib import Path

    image_dir = Path(__file__).resolve().parents[1] / "image"
    messages = []
    for template in sorted(image_dir.glob("*.pkr.hcl")):
        messages.extend(
            (template.name, m)
            for m in re.findall(r'error_message\s*=\s*"([^"]*)"', template.read_text())
        )
    assert messages, "expected validation messages in the Packer templates"
    for name, message in messages:
        assert re.fullmatch(r"[A-Z].*[.?]", message), (
            f"{name}: {message!r} must start uppercase and end with . or ?"
        )
