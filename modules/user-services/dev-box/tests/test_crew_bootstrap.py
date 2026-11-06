"""User-data Crew bootstrap (#8055): the rendered first-boot script is valid shell, pins
every input it fetches, installs exactly what image/crew.pkr.hcl installs and carries
no secret. Terraform renders infra/crew-bootstrap.sh.tftpl; this renders it the same way
offline so `bash -n` and the content checks run without a provider.
"""

import re
import subprocess
from pathlib import Path

import pytest

MODULE_DIR = Path(__file__).resolve().parents[1]
TEMPLATE = MODULE_DIR / "infra" / "crew-bootstrap.sh.tftpl"
TERRAFORM = MODULE_DIR / "infra" / "crew_bootstrap.tf"
PACKER = MODULE_DIR / "image" / "crew.pkr.hcl"
HELPERS = ("install_crew.py", "write_enrollment.py", "write_host_certificate.py", "verify_crew_ready.py",
           "restore_crew_host.py", "checkpoint_crew_host.py", "crew_checkpoint.py")

VALUES = {
    "bundle_s3_uri": "s3://adp-terraform-state-000000000000/dev-box/image-inputs/" + "c" * 64 + "/crew.tar.gz",
    "manifest_s3_uri": "s3://adp-terraform-state-000000000000/dev-box/image-inputs/" + "c" * 64 + "/profile.json",
    "helper_s3_prefix": "s3://adp-terraform-state-000000000000/dev-box/image-inputs/" + "c" * 64 + "/adp",
    "helper_pins": " ".join(f"{name}={index:064x}" for index, name in enumerate(HELPERS)),
    "bundle_sha256": "c" * 64,
    "profile_revision": "crew-r1",
    "source_revision": "b" * 40,
    "region": "us-east-1",
}


def render(values=VALUES) -> str:
    """templatefile() semantics for the subset the template uses: ${name} and the $${ escape."""
    source = TEMPLATE.read_text()
    escaped = source.replace("$${", "\x00")
    unknown = set(re.findall(r"\$\{([^}]*)\}", escaped)) - set(values)
    assert not unknown, f"template references undeclared variables: {unknown}"
    rendered = re.sub(r"\$\{(\w+)\}", lambda match: values[match.group(1)], escaped)
    assert "%{" not in rendered, "template directives are not used; keep the template renderable offline"
    return rendered.replace("\x00", "${")


@pytest.fixture(scope="module")
def script() -> str:
    return render()


def test_rendered_bootstrap_is_valid_bash(script, tmp_path):
    path = tmp_path / "crew-bootstrap.sh"
    path.write_text(script)
    result = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert script.startswith("#!/bin/bash\n")
    assert "set -euo pipefail" in script and "umask 077" in script


def test_bootstrap_verifies_every_download_before_use(script):
    fetch_lines = [line.strip() for line in script.splitlines() if line.strip().startswith("fetch ")]
    assert fetch_lines == [
        'fetch "$BUNDLE_URI" "$WORK/crew.tar.gz"',
        'fetch "$MANIFEST_URI" "$WORK/profile.json"',
        'fetch "$HELPER_PREFIX/$name" "$WORK/adp/$name"',
    ]
    assert script.index('echo "$BUNDLE_SHA256  $WORK/crew.tar.gz" | sha256sum -c --quiet -') < script.index('install_crew.py" --manifest')
    assert 'echo "$digest  $WORK/adp/$name" | sha256sum -c --quiet -' in script
    assert "aws s3 cp --region \"$REGION\" --only-show-errors" in script
    for forbidden in ("curl ", "wget ", "latest", "pip install", "npm install", "dnf install", "yum install"):
        assert forbidden not in script, forbidden


def test_bootstrap_uses_the_single_installer_with_both_expected_revisions(script):
    assert ('"$GUEST_PYTHON" "$WORK/adp/install_crew.py" --manifest "$WORK/profile.json" --bundle "$WORK/crew.tar.gz" \\\n'
            '  --expected-source-revision "$SOURCE_REVISION" --expected-profile-revision "$PROFILE_REVISION"') in script
    assert script.count("install_crew.py") == 1 + VALUES["helper_pins"].count("install_crew.py")
    assert "PROFILE_REVISION='crew-r1'" in script and f"SOURCE_REVISION='{'b' * 40}'" in script
    assert f"BUNDLE_SHA256='{'c' * 64}'" in script


def test_bootstrap_installs_the_same_root_only_helpers_as_the_crew_image(script):
    packer = PACKER.read_text()
    installs = re.findall(r'"sudo install -m (0[0-7]{3}) -o root -g root /tmp/adp-[a-z-]+\.py (/opt/adp/crew/([a-z_]+\.py))"', packer)
    assert len(installs) == 6, installs
    for mode, destination, name in installs:
        assert f'install -m {mode} -o root -g root "$WORK/adp/{name}" {destination}' in script, name
    assert "openssl version >/dev/null" in script
    helper_lines = re.findall(r'^install -m 0[0-7]{3} -o root -g root "\$WORK/adp/[a-z_]+\.py" /opt/adp/crew/[a-z_]+\.py$', script, re.M)
    assert len(helper_lines) == 6


def test_terraform_pins_exactly_the_helpers_the_crew_image_ships():
    terraform = TERRAFORM.read_text()
    pinned = dict(re.findall(r'"([a-z_]+\.py)"\s+= filesha256\("\$\{path\.module\}/(\S+)"\)', terraform))
    assert set(pinned) == set(HELPERS)
    for name, relative in pinned.items():
        assert (MODULE_DIR / "infra" / relative).resolve().is_file(), relative
        assert relative.endswith("/" + name)
    packer_sources = set(re.findall(r'source\s+= "\$\{path\.root\}/(?:\.\./)*[^"]*?([a-z_]+\.py)"', PACKER.read_text()))
    assert packer_sources == set(HELPERS)
    assert 'user_data = local.crew_bootstrap_user_data' in (MODULE_DIR / "infra" / "main.tf").read_text()
    assert 'crew_user_data_bootstrap ? base64encode(templatefile("${path.module}/crew-bootstrap.sh.tftpl"' in terraform
    assert '"arn:aws:s3:::${local.crew_bootstrap_bucket}/dev-box/image-inputs/*"' in terraform
    assert "kms:" not in terraform


def test_bootstrap_runs_the_readiness_preflight_and_records_a_receipt(script):
    assert "STEP=preflight" in script
    assert "python3 -I /opt/adp/crew/verify_crew_ready.py --help >/dev/null" in script
    assert "python3 -I -c 'import sys; sys.path.insert(0, \"/opt/adp/crew\"); import checkpoint_crew_host, restore_crew_host, write_enrollment, write_host_certificate'" in script
    assert ("systemctl cat adp-crew.service adp-crew-host.service adp-crew-checkpoint.service adp-crew-checkpoint.timer "
            "adp-crew-checkpoint-final.service >/dev/null") in script
    for required in ("/etc/systemd/system/adp-crew.service", "/opt/adp/crew/current/venv/bin/kirocrew",
                     "/opt/adp/crew/current/host/crew-host-bootstrap.js"):
        assert required in script
    assert '"/run/adp/crew-bootstrap.json"' in script and '"/var/lib/adp/crew-bootstrap.json"' in script
    assert "C /run/adp/crew-bootstrap.json 0600 root root - /var/lib/adp/crew-bootstrap.json" in script
    assert 'write_marker ok "crew profile installed from the pinned bundle"' in script
    assert "trap on_error ERR" in script and 'write_marker failed "exit $code at step $STEP"' in script
    assert script.index("write_marker ok") > script.index("STEP=preflight")
    assert "os.fchmod(out.fileno(), 0o600)" in script
    assert "systemctl enable" not in script and "systemctl start" not in script


def test_bootstrap_carries_no_secret_shaped_content(script):
    for pattern in (r"AKIA[0-9A-Z]{16}", r"(?i)aws_secret_access_key", r"(?i)session_token", r"PRIVATE KEY",
                    r"(?i)x-amz-security-token", r"(?i)bearer ", r"(?i)api[_-]?key"):
        assert re.search(pattern, script) is None, pattern
    assert "${" not in script.replace("${", "", 0) or "$${" not in script


def test_bootstrap_does_not_leave_unescaped_terraform_interpolation():
    source = TEMPLATE.read_text()
    assert "%{" not in source
    shell_expansions = re.findall(r"(?<!\$)\$\{([^}]*)\}", source)
    assert set(shell_expansions) <= set(VALUES), f"bash parameter expansions must be written as $${{...}}: {set(shell_expansions) - set(VALUES)}"


def test_bootstrap_prefers_the_bundled_interpreter_and_falls_back_to_system_python(script):
    """#8055 live failure: the AL2023 system python3 is 3.9. The helpers run on the bundle's
    relocatable CPython 3.12 when the digest-verified tarball carries python/bin/python3,
    staged out before install_crew.py (which is what extracts the rest), else on python3."""
    select = script[script.index("STEP=select-python"):script.index("STEP=fetch-helpers")]
    assert script.index("sha256sum -c --quiet -") < script.index("STEP=select-python") < script.index('install_crew.py" --manifest')
    assert "GUEST_PYTHON=python3" in script[:script.index("STEP=select-python")], "the fallback is the system interpreter"
    assert "tar -tzf \"$WORK/crew.tar.gz\" | grep -Eqx 'python/bin/python3(\\.[0-9]+)?'" in select
    assert 'tar -xzf "$WORK/crew.tar.gz" -C "$WORK/python-stage" --no-same-owner python' in select
    assert "sys.exit(sys.version_info < (3, 12))" in select, "a staged interpreter older than 3.12 is not trusted"
    assert 'GUEST_PYTHON="$candidate"' in select
    assert "GUEST_PYTHON_VERSION=$(\"$GUEST_PYTHON\" -I -c" in select
    assert '"interpreter": os.environ.get("GUEST_PYTHON_VERSION", "")' in script, "the receipt records the interpreter"
    assert 'export GUEST_PYTHON_VERSION=""' in script[:script.index("write_marker()")], "running/failed receipts carry the field too"
    assert '"$GUEST_PYTHON" - "$PROFILE_REVISION" "$SOURCE_REVISION"' in script
    # The system interpreter is what the units and the controller use; the preflight proves the helpers load there.
    assert "python3 -I /opt/adp/crew/verify_crew_ready.py --help" in script
    assert "python/bin/python3" not in script[script.index("STEP=install-helpers"):script.index("STEP=preflight")]
    # The staged interpreter lives under $WORK and goes with it.
    assert script.rindex('rm -rf "$WORK"') > script.index("STEP=preflight")


def test_packer_crew_image_makes_the_same_interpreter_choice():
    source = PACKER.read_text()
    for required in (
        '"sudo install -d -m 0700 -o root -g root /var/tmp/adp-crew-inputs/python-stage"',
        "if sudo tar -tzf /var/tmp/adp-crew-inputs/crew.tar.gz | grep -Eqx 'python/bin/python3(\\\\.[0-9]+)?'; then sudo tar -xzf /var/tmp/adp-crew-inputs/crew.tar.gz -C /var/tmp/adp-crew-inputs/python-stage --no-same-owner python; fi",
        "GUEST_PYTHON=python3; if sudo test -x /var/tmp/adp-crew-inputs/python-stage/python/bin/python3; then GUEST_PYTHON=/var/tmp/adp-crew-inputs/python-stage/python/bin/python3; fi",
        'sudo \\"$GUEST_PYTHON\\" /tmp/adp-install-crew.py --manifest /tmp/adp-crew-profile.json --bundle /var/tmp/adp-crew-inputs/crew.tar.gz --expected-source-revision ${var.source_revision} --expected-profile-revision ${var.profile_revision}',
        '"sudo python3 -I /opt/adp/crew/verify_crew_ready.py --help >/dev/null"',
        "import checkpoint_crew_host, restore_crew_host, write_enrollment, write_host_certificate",
    ):
        assert required in source, required
    assert "sudo python3 /tmp/adp-install-crew.py" not in source
    assert source.index("sha256sum -c --quiet -") < source.index("python-stage") < source.index("/tmp/adp-install-crew.py --manifest")
    assert source.index("verify_crew_ready.py --help") < source.index('"sudo rm -rf /var/tmp/adp-crew-inputs"')
