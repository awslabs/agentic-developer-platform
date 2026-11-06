"""Attest a pending host enrollment through owned EC2 and SSM observations."""

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import shlex
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import select, update

from src.agentauth.chat_crew_run import CrewRunConflictError
from src.cloud_desktop.host_identity import current_host, ec2_crew_allowed, store_host_fingerprint
from src.shared.models.crew_checkpoint import CrewCheckpoint
from src.shared.models.crew_executor import CrewExecutor
from src.shared.models.base import new_uuid, utcnow
from src.shared.models.dev_box import (
    DevBox,
    DevBoxAccessSession,
    DevBoxDevice,
    DevBoxEvent,
    DevBoxInstance,
    DevBoxOperation,
    DevBoxPolicy,
    DevBoxResource,
)
from src.shared.models.onboarding import TenantMembership

from controller.launch import ReleasePins, ownership_tags, private_host_address, release_pins
from controller.state import owned_instance

logger = logging.getLogger(__name__)

ATTESTATION_TIMEOUT_SECONDS = 30
ATTESTATION_POLL_SECONDS = 1


def crew_profile_command(profile_revision, source_revision, bundle_sha256=None):
    """Secret-free SSM readback of the installed Crew profile.

    With ``bundle_sha256`` (user-data bootstrap, #8055) the host must also present the
    root-owned first-boot receipt /run/adp/crew-bootstrap.json recording status "ok"
    for exactly this profile revision, source revision and bundle digest; a missing,
    failed, foreign or writable receipt fails closed before the profile is examined.

    Every refusal names itself on stderr as ``Crew host profile refused: <Class>: <detail>``
    so ``record_host_refusal`` can carry the class to the owner's page (#8223 cause 34: a
    bare ``sys.exit(1)`` read as "lease fenced" for an hour while the controller pinned a
    release the box was never provisioned from).
    """
    receipt = "" if bundle_sha256 is None else (
        "import os,stat\n"
        "marker=Path('/run/adp/crew-bootstrap.json')\n"
        "if marker.is_symlink() or not marker.is_file(): refuse('CrewReceiptMissing', str(marker))\n"
        "entry=marker.lstat()\n"
        "if not (stat.S_ISREG(entry.st_mode) and entry.st_uid==0 and not entry.st_mode & 0o022): "
        "refuse('CrewReceiptUntrusted', str(marker))\n"
        "receipt=json.loads(marker.read_text())\n"
        "if receipt.get('status')!='ok' or receipt.get('crew_bootstrap')!='user-data': "
        "refuse('CrewReceiptRefused', str(receipt.get('status')))\n"
        "got=(receipt.get('profile_revision'), receipt.get('source_revision'), receipt.get('bundle_sha256'))\n"
        "if got!=(sys.argv[1], sys.argv[2], sys.argv[3]): "
        "refuse('CrewProfileMismatch', 'expected='+sys.argv[1]+' got='+str(got[0]))\n"
    )
    code = (
        "import json,sys\nfrom pathlib import Path\n"
        "def refuse(kind, detail):\n"
        "    print('Crew host profile refused: '+kind+': '+detail, file=sys.stderr)\n"
        "    sys.exit(1)\n"
        + receipt +
        "current=Path('/opt/adp/crew/current')\n"
        "installed=str(current.readlink()) if current.is_symlink() else 'none'\n"
        "if installed!=sys.argv[1]: refuse('CrewProfileMismatch', 'expected='+sys.argv[1]+' got='+installed)\n"
        "profile=json.loads((current/'profile.json').read_text())\n"
        "if profile.get('revision')!=sys.argv[1] or profile.get('source_revision')!=sys.argv[2]: "
        "refuse('CrewProfileMismatch', 'expected='+sys.argv[1]+' got='+str(profile.get('revision')))\n"
        "required=('/etc/systemd/system/adp-crew.service', "
        "'/etc/systemd/system/adp-crew-host.service', "
        "'/opt/adp/crew/current/venv/bin/kirocrew', "
        "'/opt/adp/crew/current/host/crew-host-dashboard.js', '/opt/adp/crew/current/host/crew-host-model.js', "
        "'/opt/adp/crew/current/host/crew-host-bootstrap.js')\n"
        "missing=[path for path in required if not Path(path).is_file()]\n"
        "if missing: refuse('CrewInstallIncomplete', ','.join(missing))\n"
    )
    digest = "" if bundle_sha256 is None else f" {shlex.quote(bundle_sha256)}"
    return (f"python3 -c {shlex.quote(code)} {shlex.quote(profile_revision)} "
            f"{shlex.quote(source_revision)}{digest} && printf 'crew-profile-ok\\n'")


RELEASE_PINS_READBACK = "crew-profile-pins"


def release_pins_discovery_command():
    """Secret-free SSM readback of the release a host was provisioned from.

    For instance rows that predate the recorded pins (#8223 cause 34) the controller learns
    them from the host's own root-owned first-boot receipt, accepted only when the installed
    profile agrees with it. Prints exactly ``crew-profile-pins <profile> <source> <bundle>``.
    """
    code = (
        "import json,os,stat,sys\nfrom pathlib import Path\n"
        "marker=Path('/run/adp/crew-bootstrap.json')\n"
        "if marker.is_symlink() or not marker.is_file(): sys.exit(1)\n"
        "entry=marker.lstat()\n"
        "if not (stat.S_ISREG(entry.st_mode) and entry.st_uid==0 and not entry.st_mode & 0o022): sys.exit(1)\n"
        "receipt=json.loads(marker.read_text())\n"
        "if receipt.get('status')!='ok' or receipt.get('crew_bootstrap')!='user-data': sys.exit(1)\n"
        "current=Path('/opt/adp/crew/current')\n"
        "profile=json.loads((current/'profile.json').read_text())\n"
        "pins=(receipt.get('profile_revision'), receipt.get('source_revision'), receipt.get('bundle_sha256'))\n"
        "if (not current.is_symlink() or str(current.readlink())!=pins[0] or profile.get('revision')!=pins[0] "
        "or profile.get('source_revision')!=pins[1] or not all(isinstance(pin, str) and pin for pin in pins)): sys.exit(1)\n"
        "print('crew-profile-pins', *pins)\n"
    )
    return f"python3 -c {shlex.quote(code)}"


_PIN_PATTERNS = (re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}"), re.compile(r"[0-9a-f]{7,40}"), re.compile(r"[0-9a-f]{64}"))


def parse_release_pins_readback(output):
    """The three pins from a discovery readback, or ``None`` for anything malformed."""
    lines = (output or "").splitlines()
    if len(lines) != 1 or not lines[0].startswith(RELEASE_PINS_READBACK + " "):
        return None
    parts = lines[0].split(" ")[1:]
    if len(parts) != 3 or any(not pattern.fullmatch(part) for pattern, part in zip(_PIN_PATTERNS, parts)):
        return None
    return tuple(parts)


def generation_pins(instance, launch_template):
    """The release a box must be verified against: the instance row's recorded pins when it has
    them, else the current release (rows from before the pins existed, and image-mode boxes)."""
    if instance is not None and getattr(instance, "profile_revision", None):
        return ReleasePins(
            profile_revision=instance.profile_revision,
            source_revision=instance.source_revision,
            crew_bundle_sha256=instance.crew_bundle_sha256,
            crew_bootstrap="user-data" if instance.crew_bundle_sha256 else "image",
            image_id=instance.image_ref,
            launch_template_version=getattr(instance, "launch_template_version", None),
        )
    return release_pins(launch_template) if launch_template is not None else None


def expected_image_id(instance, launch_template):
    """The AMI the machine must run: its own once the generation is pinned, else the release's."""
    if instance is not None and getattr(instance, "profile_revision", None):
        return instance.image_ref
    return launch_template.image_id if launch_template is not None else None


async def discover_release_pins(ssm, instance_id):
    try:
        command = await asyncio.to_thread(
            ssm.send_command, InstanceIds=[instance_id], DocumentName="AWS-RunShellScript",
            Parameters={"commands": [release_pins_discovery_command()]},
            CloudWatchOutputConfig={"CloudWatchOutputEnabled": False},
        )
        async with asyncio.timeout(ATTESTATION_TIMEOUT_SECONDS):
            while True:
                try:
                    result = await asyncio.to_thread(
                        ssm.get_command_invocation, CommandId=command["Command"]["CommandId"], InstanceId=instance_id,
                    )
                except ClientError as error:
                    if error.response.get("Error", {}).get("Code") != "InvocationDoesNotExist":
                        raise
                else:
                    if result.get("Status") not in {"Pending", "InProgress", "Delayed"}:
                        break
                await asyncio.sleep(ATTESTATION_POLL_SECONDS)
    except (TimeoutError, OSError, BotoCoreError, ClientError):
        return None
    if result.get("Status") != "Success":
        return None
    return parse_release_pins_readback(result.get("StandardOutputContent", ""))


async def resolve_generation_pins(db, ssm, instance, launch_template):
    """The pins to verify ``instance`` against, learning them from the host once for a row
    that predates the recorded pins. A failed discovery leaves the row alone and verifies
    against the current release, exactly as before."""
    if (instance is None or getattr(instance, "profile_revision", None) or launch_template is None
            or launch_template.crew_bootstrap != "user-data" or not instance.provider_instance_id):
        return generation_pins(instance, launch_template)
    discovered = await discover_release_pins(ssm, instance.provider_instance_id)
    if discovered is None:
        return generation_pins(instance, launch_template)
    instance.profile_revision, instance.source_revision, instance.crew_bundle_sha256 = discovered
    instance.launch_template_version = None
    await db.commit()
    logger.info(
        "dev-box generation pins recorded from the host receipt: instance=%s profile=%s source=%s",
        instance.provider_instance_id, instance.profile_revision, instance.source_revision,
    )
    return generation_pins(instance, launch_template)


def crew_source_revision(launch_template):
    """The commit the installed profile must declare: the bundle's own source revision
    in user-data mode (the base AMI was built from another commit), else the release's.
    A ``ReleasePins`` already carries the derived value as ``source_revision``."""
    return getattr(launch_template, "crew_source_revision", None) or launch_template.source_revision


def host_profile_command(launch_template):
    """The profile readback for this release's bootstrap mode; fails closed when a
    user-data release lacks its bundle digest."""
    if launch_template.crew_bootstrap == "user-data":
        if not launch_template.crew_bundle_sha256:
            return "false"
        return crew_profile_command(launch_template.profile_revision, crew_source_revision(launch_template),
                                    bundle_sha256=launch_template.crew_bundle_sha256)
    return crew_profile_command(launch_template.profile_revision, crew_source_revision(launch_template))


WORKSPACE_HOME = "/mnt/crew"
WORKSPACE_LABEL = "adp-crew"
# The host's root-only state directory inside the Crew home (restore_crew_host.MARKER_DIRECTORY,
# crew_checkpoint.EXCLUDED_ROOTS): never archived, never counted as Crew content. Everything the
# host keeps on the retained disk for the owner lives under it, so a fresh disk still reads as an
# empty Crew home to the first-boot restore.
HOST_STATE_DIRECTORY = f"{WORKSPACE_HOME}/.adp-host"
OWNER_USER = "ec2-user"
OWNER_HOME = f"/home/{OWNER_USER}"
OWNER_HOME_STORE = f"{HOST_STATE_DIRECTORY}/home/{OWNER_USER}"
SHARED_WORKSPACE = "/workspace"
SHARED_WORKSPACE_STORE = f"{HOST_STATE_DIRECTORY}/workspace"
SHARED_GROUP = "crew"


def workspace_mount_command(volume_id):
    """Shell that makes the owner's retained workspace disk the Crew home before readiness.

    The controller attaches the disk (storage.py, /dev/xvdf) but nothing on the guest ever
    formatted or mounted it: dev 2026-10-19 the 100 GB volume sat on the box as a blank nvme
    device, /mnt/crew did not exist, and restore_crew_host.py failed before Crew could start
    (#8223). The device is addressed by its EBS volume id (Nitro exposes it as the NVMe
    serial), so a wrong disk can never be touched. It is formatted ONLY when it carries no
    filesystem at all; any other filesystem fails the chain and nothing is overwritten. The
    mount is idempotent and recorded in fstab (nofail) so a reboot keeps it. An empty home is
    handed back to root (the restore helper's trust check; a failed restore leaves it owned by
    agent), a home with content is never touched. Prints nothing on success so the readiness
    readback stays exactly "crew-profile-ok..." lines.
    """
    if not re.fullmatch(r"vol-[0-9a-f]{8,17}", volume_id or ""):
        return "false"
    device = f"/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_{volume_id.replace('-', '', 1)}"
    fstab = f"LABEL={WORKSPACE_LABEL} {WORKSPACE_HOME} xfs noatime,nodev,nosuid,nofail 0 2"
    return (
        f"WS={shlex.quote(device)}; test -b \"$WS\""
        f" && {{ [ \"$(blkid -o value -s TYPE \"$WS\")\" = xfs ] || {{ [ -z \"$(blkid -o value -s TYPE \"$WS\")\" ]"
        f" && mkfs.xfs -q -L {WORKSPACE_LABEL} \"$WS\" >/dev/null; }}; }}"
        f" && mkdir -p -m 0755 {WORKSPACE_HOME}"
        f" && {{ mountpoint -q {WORKSPACE_HOME} || mount -t xfs -o noatime,nodev,nosuid \"$WS\" {WORKSPACE_HOME}; }}"
        f" && {{ grep -q ' {WORKSPACE_HOME} ' /etc/fstab || printf '%s\\n' {shlex.quote(fstab)} >> /etc/fstab; }}"
        # #8835 disk growth: the controller enlarges the retained volume in place (storage.py grow_volume);
        # the filesystem follows here. A disk that did not grow is a no-op ("data size unchanged").
        f" && {{ xfs_growfs -d {WORKSPACE_HOME} >/dev/null 2>&1 || true; }}"
        # restore_crew_host.py needs an EMPTY home owned by root; a failed restore leaves an
        # empty home owned by agent (it chowns before configuring), so hand it back to root.
        # A home with content is never touched. The host's own `.adp-host` state (SSH host keys,
        # the owner's home, the shared workspace) is not Crew content and does not count.
        f" && {{ [ -n \"$(ls -A {WORKSPACE_HOME} | grep -vx .adp-host)\" ] || chown root:root {WORKSPACE_HOME}; }}"
    )


SSH_HOST_KEY_STORE = f"{HOST_STATE_DIRECTORY}/ssh-host-keys"
LEGACY_SSH_HOST_KEY_STORE = f"{WORKSPACE_HOME}/.adp-ssh-host-keys"  # dev gen 55 only


def ssh_host_key_command():
    """Keep one SSH host identity per box across generations.

    Every Replace launches a new instance with freshly generated host keys, so the owner's next
    `adp devbox ssh` met OpenSSH's "HOST KEY HAS CHANGED" warning (dev 2026-10-26) — the warning the
    setup text tells them never to bypass, and the fingerprint an operator is supposed to publish could
    never be stable. The keys live root-only on the retained workspace disk: a generation that finds
    them installs them (owner root:ssh_keys 0640, SELinux label restored) and restarts sshd only when
    something changed; the first generation stores the keys it generated. Runs after the mount, prints
    nothing and never fails the readiness chain.
    """
    store = SSH_HOST_KEY_STORE
    return (
        f"{{ install -d -m 0700 -o root -g root {HOST_STATE_DIRECTORY}"
        f" && {{ [ ! -d {LEGACY_SSH_HOST_KEY_STORE} ] || [ -d {store} ] || mv {LEGACY_SSH_HOST_KEY_STORE} {store}; }}"
        f" && install -d -m 0700 -o root -g root {store}"
        f" && if ls {store}/ssh_host_*_key >/dev/null 2>&1; then changed=0;"
        f" for key in {store}/ssh_host_*_key; do name=$(basename \"$key\");"
        f" cmp -s \"$key\" /etc/ssh/$name || {{ cp -f \"$key\" \"$key.pub\" /etc/ssh/ && changed=1; }}; done;"
        f" chown root:ssh_keys /etc/ssh/ssh_host_*_key && chmod 0640 /etc/ssh/ssh_host_*_key && chmod 0644 /etc/ssh/ssh_host_*_key.pub;"
        f" command -v restorecon >/dev/null 2>&1 && restorecon -F /etc/ssh/ssh_host_*;"
        f" [ \"$changed\" = 0 ] || systemctl restart sshd;"
        f" else cp -p /etc/ssh/ssh_host_*_key /etc/ssh/ssh_host_*_key.pub {store}/; fi; }} >/dev/null 2>&1 || true"
    )


def owner_home_command():
    """Give the owner's SSH login the retained disk: their home and a shared workspace.

    Developers log in over SSH and run Claude Code or Codex under tmux (owner, 2026-10-26). Until
    now `/home/ec2-user` sat on the 16 GB root volume and vanished with every Replace, while the
    retained disk was the Crew agent's home alone (mode 0700). The owner's home now lives at
    `.adp-host/home/ec2-user` on the retained disk, bind-mounted over `/home/ec2-user` (recorded in
    fstab, nofail) — seeded once from the image's skeleton (.tmux.conf, Codex and Claude Code
    settings), afterwards theirs. `/workspace` is a shared directory on the same disk (setgid
    group `crew`: the owner and the Crew agent) so checkouts are the same for the SSH session,
    Kiro Crew, Codex and Claude Code. Everything sits under the host's `.adp-host` state directory,
    which checkpoints exclude and the first-boot restore ignores. Prints nothing; never fails the
    readiness chain.
    """
    return (
        f"{{ install -d -m 0700 -o root -g root {HOST_STATE_DIRECTORY} {HOST_STATE_DIRECTORY}/home"
        f" && {{ getent group {SHARED_GROUP} >/dev/null || groupadd -r {SHARED_GROUP}; }}"
        f" && usermod -aG {SHARED_GROUP} {OWNER_USER} && usermod -aG {SHARED_GROUP} agent"
        f" && {{ [ -d {OWNER_HOME_STORE} ] || {{ install -d -m 0700 -o {OWNER_USER} -g {OWNER_USER} {OWNER_HOME_STORE}"
        f" && cp -a {OWNER_HOME}/. {OWNER_HOME_STORE}/; }}; }}"
        f" && {{ mountpoint -q {OWNER_HOME} || mount --bind {OWNER_HOME_STORE} {OWNER_HOME}; }}"
        f" && {{ grep -q ' {OWNER_HOME} ' /etc/fstab || printf '%s\\n' '{OWNER_HOME_STORE} {OWNER_HOME} none bind,nofail 0 0' >> /etc/fstab; }}"
        f" && install -d -m 2775 -o {OWNER_USER} -g {SHARED_GROUP} {SHARED_WORKSPACE_STORE} && install -d -m 0755 {SHARED_WORKSPACE}"
        f" && {{ mountpoint -q {SHARED_WORKSPACE} || mount --bind {SHARED_WORKSPACE_STORE} {SHARED_WORKSPACE}; }}"
        f" && {{ grep -q ' {SHARED_WORKSPACE} ' /etc/fstab || printf '%s\\n' '{SHARED_WORKSPACE_STORE} {SHARED_WORKSPACE} none bind,nofail 0 0' >> /etc/fstab; }}"
        f" && {{ ! command -v restorecon >/dev/null 2>&1 || restorecon -R {OWNER_HOME} {SHARED_WORKSPACE}; }}; }} >/dev/null 2>&1 || true"
    )


def crew_key_command():
    code = (
        "import os,stat,subprocess,sys; from pathlib import Path; "
        "private=Path('/run/adp/dev-box-private-key'); public=Path('/run/adp/dev-box-public-key'); "
        "private_stat=private.lstat(); public_stat=public.lstat(); "
        "safe=all(stat.S_ISREG(item.st_mode) and item.st_uid==0 and not item.st_mode & 0o077 "
        "for item in (private_stat,public_stat)); safe or sys.exit(1); "
        "derived=subprocess.run(['openssl','pkey','-in',str(private),'-pubout'],capture_output=True,check=False); "
        "sys.exit(derived.returncode!=0 or derived.stdout!=public.read_bytes())"
    )
    return f"python3 -c {shlex.quote(code)} && printf 'crew-key-ok\\n'"


async def reconcile_host_key(session_factory, ec2, ssm, instance_id, *, launch_template):
    if (launch_template is None or not launch_template.profile_revision
            or not launch_template.source_revision):
        return False
    async with session_factory() as db:
        instance = await db.get(DevBoxInstance, instance_id)
        box = await db.get(DevBox, instance.box_id) if instance else None
        if (box is None or box.profile != "crew" or instance.enrollment_public_key
                or not await ec2_crew_allowed(db, box.org_id)
                or not release_image_matches(box, instance, launch_template)
                or await current_host(db, box_id=box.id, generation=box.machine_generation,
                                      provider_instance_id=instance.provider_instance_id,
                                      user_id=box.user_id, org_id=box.org_id,
                                      deployment_id=box.deployment_id) is None):
            return False
        try:
            observed = await asyncio.to_thread(owned_instance, ec2, instance.provider_instance_id, ownership_tags(box, box))
        except ValueError:
            return False
        if observed.get("State", {}).get("Name") != "running" or observed.get("ImageId") != expected_image_id(instance, launch_template):
            return False
        command = await asyncio.to_thread(
            ssm.send_command,
            InstanceIds=[instance.provider_instance_id], DocumentName="AWS-RunShellScript",
            Parameters={"commands": [
                host_profile_command(generation_pins(instance, launch_template)),
                "python3 /opt/adp/crew/write_enrollment.py --generate-key",
                crew_key_command(),
            ]},
            CloudWatchOutputConfig={"CloudWatchOutputEnabled": False},
        )
        try:
            async with asyncio.timeout(ATTESTATION_TIMEOUT_SECONDS):
                while True:
                    try:
                        result = await asyncio.to_thread(
                            ssm.get_command_invocation, CommandId=command["Command"]["CommandId"],
                            InstanceId=instance.provider_instance_id,
                        )
                    except ClientError as error:
                        if error.response.get("Error", {}).get("Code") != "InvocationDoesNotExist":
                            raise
                    else:
                        if result.get("Status") not in {"Pending", "InProgress", "Delayed"}:
                            break
                    await asyncio.sleep(ATTESTATION_POLL_SECONDS)
        except TimeoutError:
            return False
        output = result.get("StandardOutputContent", "")
        if (result.get("Status") != "Success" or not output.startswith("crew-profile-ok\n")
                or not output.endswith("crew-key-ok\n")):
            return False
        public_key = output[len("crew-profile-ok\n"):-len("crew-key-ok\n")]
        if (not 512 <= len(public_key) <= 2048
                or not public_key.startswith("-----BEGIN PUBLIC KEY-----\n")
                or not public_key.endswith("-----END PUBLIC KEY-----\n")):
            return False
        await db.refresh(box)
        await db.refresh(instance)
        if (instance.enrollment_public_key or not release_image_matches(box, instance, launch_template)
                or await current_host(db, box_id=box.id, generation=box.machine_generation,
                                      provider_instance_id=instance.provider_instance_id,
                                      user_id=box.user_id, org_id=box.org_id,
                                      deployment_id=box.deployment_id) is None):
            return False
        instance.enrollment_public_key = public_key.encode("ascii")
        await db.commit()
        return True


async def reconcile_host_fingerprint(session_factory, ec2, ssm, instance_id, *, launch_template):
    if not launch_template or not launch_template.profile_revision or not launch_template.source_revision:
        return False
    async with session_factory() as db:
        instance = await db.get(DevBoxInstance, instance_id)
        box = await db.get(DevBox, instance.box_id) if instance else None
        if (box is None or box.profile != "crew" or instance.certificate_fingerprint
                or not release_image_matches(box, instance, launch_template)
                or not await ec2_crew_allowed(db, box.org_id)
                or await current_host(db, box_id=box.id, generation=box.machine_generation,
                                      provider_instance_id=instance.provider_instance_id, user_id=box.user_id,
                                      org_id=box.org_id, deployment_id=box.deployment_id) is None):
            return False
        try:
            observed = await asyncio.to_thread(owned_instance, ec2, instance.provider_instance_id, ownership_tags(box, box))
        except ValueError:
            return False
        address = private_host_address(observed)
        if (observed.get("State", {}).get("Name") != "running"
                or observed.get("ImageId") != expected_image_id(instance, launch_template)
                or not address or address != instance.private_address):
            return False
        pins = await resolve_generation_pins(db, ssm, instance, launch_template)
        command = await asyncio.to_thread(
            ssm.send_command, InstanceIds=[instance.provider_instance_id], DocumentName="AWS-RunShellScript",
            Parameters={"commands": [
                f"set -e; {host_profile_command(pins)}"
                f" && python3 /opt/adp/crew/write_host_certificate.py --private-ip {shlex.quote(address)}"
                f" --generation {box.machine_generation} --fingerprint",
            ]}, CloudWatchOutputConfig={"CloudWatchOutputEnabled": False},
        )
        try:
            async with asyncio.timeout(ATTESTATION_TIMEOUT_SECONDS):
                while True:
                    try:
                        result = await asyncio.to_thread(
                            ssm.get_command_invocation, CommandId=command["Command"]["CommandId"],
                            InstanceId=instance.provider_instance_id,
                        )
                    except ClientError as error:
                        if error.response.get("Error", {}).get("Code") != "InvocationDoesNotExist":
                            raise
                    else:
                        if result.get("Status") not in {"Pending", "InProgress", "Delayed"}:
                            break
                    await asyncio.sleep(ATTESTATION_POLL_SECONDS)
        except TimeoutError:
            return False
        output = result.get("StandardOutputContent", "")
        if result.get("Status") != "Success" or not re.fullmatch(r"crew-profile-ok\n[0-9a-f]{64}\n", output):
            return False
        if not await store_host_fingerprint(
            db, box_id=box.id, generation=box.machine_generation, provider_instance_id=instance.provider_instance_id,
            user_id=box.user_id, org_id=box.org_id, deployment_id=box.deployment_id, fingerprint=output.splitlines()[1],
        ):
            return False
        await db.commit()
        return True


DASHBOARD_ORIGIN_ENV = "ADP_CREW_DASHBOARD_ORIGIN"


def crew_dashboard_origin(environment=None):
    """The Crew edge origin the host must render into Kiro Crew's ``dashboard.url``, or ``None``.

    The controller shares the gateway ConfigMap (CREW_DASHBOARD_ORIGIN). Kiro Crew's host
    validation admits only Host headers derived from ``dashboard.url``; the browser arrives
    through the gateway edge, so 127.0.0.1 there meant "Host header not allowed" (#8223 cause 20).
    """
    origin = (os.environ if environment is None else environment).get("CREW_DASHBOARD_ORIGIN", "").rstrip("/")
    return origin if re.fullmatch(r"https://[a-z0-9][a-z0-9.-]{0,252}", origin) else None


CREW_DASHBOARD_ORIGIN_MISSING = "crew_dashboard_origin_missing"


def crew_dashboard_feature_enabled(environment=None):
    """Whether the gateway serves the Crew dashboard (``FEATURE_CREW_DASHBOARD`` = literal ``true``).

    Rendered into the shared ConfigMap from SSM ``feature-crew-dashboard`` by both gateway
    renderers, so the controller sees the same switch the gateway Deployment does.
    """
    return (os.environ if environment is None else environment).get("FEATURE_CREW_DASHBOARD", "").strip() == "true"


def dashboard_origin_refusal(environment=None):
    """``crew_dashboard_origin_missing`` when the dashboard is on but no valid origin is configured.

    A fresh box on dev (2026-10-20, #8223 cause 20) was configured with ``dashboard.url =
    http://127.0.0.1:5476`` because the controller Deployment only reads the shared ConfigMap and
    the origin was rendered solely into the gateway Deployment env; the browser then got Kiro
    Crew's "Host header not allowed". Configuring a loopback dashboard behind the edge is never
    right while the feature is on, so readiness refuses by name and retries on the next tick.
    With the feature off, no origin is passed, as before.
    """
    if crew_dashboard_feature_enabled(environment) and crew_dashboard_origin(environment) is None:
        return CREW_DASHBOARD_ORIGIN_MISSING
    return None


HOST_REFUSAL_PREFIX = "host_refused:"


def host_refusal_class(stderr: str, status=None) -> str:
    """``host_refused:<Class>`` from the host's stderr: the innermost named class.

    The bootstrap prints ``Crew host bootstrap refused: <Class>: <message>`` and wraps the
    restorer's own line as ``... failed (<Class>: <message>)``; the LAST class named is the
    most specific (``InvalidCheckpoint`` rather than the bootstrap's generic ``Error``). The
    class is a bounded identifier (letters only), so it can travel on the instance row and in
    the box status without carrying any host output. A command that never produced a line is
    recorded by its SSM status (``host_refused:Failed``, ``host_refused:TimedOut``).
    """
    matches = re.findall(r"(?:refused: |failed \()([A-Za-z]+)", stderr or "")
    detail = matches[-1] if matches else (str(status) if status and status != "Success" else "unexpected_output")
    return (HOST_REFUSAL_PREFIX + re.sub(r"[^A-Za-z_]", "", detail))[:64]


async def record_host_refusal(db, box, instance, stderr: str, status=None) -> str:
    """Name the host's readiness refusal on the instance row (and once per class as an event).

    Cleared by the readiness success path; the owner's page reads it as
    ``host_readiness_refusal`` so a fenced lease says why (dev 2026-10-20: a resume refused for
    twelve minutes read only "lease fenced"). Returns the recorded class.
    """
    refusal_class = host_refusal_class(stderr, status)
    await db.refresh(instance)
    if instance.error_class != refusal_class:
        instance.error_class = refusal_class
        await record_readiness_refusal(db, box, refusal_class[:32])
        await db.commit()
    return refusal_class


async def record_readiness_refusal(db, box, event_type):
    """Record a named readiness refusal once per generation, on the box's latest operation.

    Events hang off operations (the schema has no box-only event); the current generation's
    most recent operation is the setup/start/replace that produced this machine. Repeated
    ticks do not repeat the event. Returns whether an event was written.
    """
    operation = await db.scalar(
        select(DevBoxOperation).where(DevBoxOperation.box_id == box.id, DevBoxOperation.machine_generation == box.machine_generation)
        .order_by(DevBoxOperation.created_at.desc()).limit(1)
    )
    if operation is None:
        return False
    already = await db.scalar(
        select(DevBoxEvent.id).where(DevBoxEvent.box_id == box.id, DevBoxEvent.machine_generation == box.machine_generation,
                                     DevBoxEvent.event_type == event_type).limit(1)
    )
    if already is not None:
        return False
    db.add(DevBoxEvent(
        id=new_uuid(), box_id=box.id, user_id=box.user_id, org_id=box.org_id, deployment_id=box.deployment_id,
        machine_generation=box.machine_generation, operation_id=operation.id, event_type=event_type, created_at=utcnow(),
    ))
    await db.commit()
    return True


def crew_bootstrap_command(launch_template, *, dashboard_origin=None):
    """The host bootstrap invocation; the dashboard origin travels as an environment variable.

    An environment prefix keeps the command valid for bundles that predate the flag: an older
    bootstrap ignores the variable, a newer one passes it to ``--configure``.
    """
    prefix = f"{DASHBOARD_ORIGIN_ENV}={shlex.quote(dashboard_origin)} " if dashboard_origin else ""
    return (
        f"{prefix}/opt/adp/crew/current/host/node /opt/adp/crew/current/host/crew-host-bootstrap.js"
        f" --gateway-arn {shlex.quote(launch_template.gateway_execute_arn)}"
        f" --model-id {shlex.quote(launch_template.crew_model_id)}"
    )


async def resume_host_executor(db, box):
    """Return the generation's parked or stale executor to ``provisioning`` for a fresh verification.

    Called where a provisioning run is minted for the box (``reconcile_enrollment``), after
    ``retire_expired_host_session`` proved no live access session remains. ``stopped`` is what
    ``park_host_executor`` leaves after an observed stop; ``ready`` covers boxes stopped before
    that leg existed (and a host whose lease simply expired). Readiness, the gateway's
    host-restore context and lease activation all key on ``provisioning`` (#8223 cause 18).
    """
    result = await db.execute(
        update(CrewExecutor)
        .where(
            CrewExecutor.tenant_id == box.org_id, CrewExecutor.user_id == box.user_id,
            CrewExecutor.target_kind == "ec2-instance", CrewExecutor.dev_box_id == box.id,
            CrewExecutor.generation == box.machine_generation, CrewExecutor.state.in_(("ready", "stopped")),
        )
        .values(state="provisioning", lease_generation=None, lease_expires_at=None)
    )
    return result.rowcount == 1


HOST_BINDING_FALLBACK = timedelta(hours=8)


def host_binding_expiry(box, now):
    """When this generation's host run and access session expire: the box's own lease end.

    A host binding used to last five minutes and be renewed by a controller tick that re-attested
    the host over SSM; any late tick (gateway rollout, controller restart, SSM delay) fenced a
    working dashboard (#8223). The binding now lasts as long as the box may run and is ended
    only by an explicit stop, replace or release (fence_host) or the box lease itself.
    """
    lease_end = box.lease_expires_at
    if lease_end is not None:
        lease_end = lease_end.replace(tzinfo=lease_end.tzinfo or UTC)
    if lease_end is None or lease_end <= now + timedelta(minutes=1):
        lease_end = now + HOST_BINDING_FALLBACK
    return lease_end


async def rebind_host_run(
    session_factory, ec2, ssm, store, box_id, *, launch_template=None
):
    """Bind a new host run to a started box on the generation its owner already approved.

    After a stop the fence revoked the run and terminated the access session but kept the
    consumed device (``fence_host(keep_devices=True)``). On the next start the machine, box and
    generation are the same, so no browser enrollment is needed: the controller mints the run and
    session itself (the sealed challenge was single-use proof at enrollment time; readiness still
    verifies the host profile, bundle and certificate before any lease is published). Returns the
    run id, or ``None`` when the box is not in that exact state.
    """
    async with session_factory() as db:
        box = await db.scalar(
            select(DevBox).where(DevBox.id == box_id).with_for_update()
        )
        if (
            box is None
            or box.profile != "crew"
            or box.status != "running"
            or box.desired_state != "running"
            or box.observed_state != "running"
            or box.auth_readiness not in ("fenced", "unknown")
            or not await ec2_crew_allowed(db, box.org_id)
        ):
            await db.rollback()
            return None
        instance = await db.scalar(
            select(DevBoxInstance).where(
                DevBoxInstance.box_id == box.id,
                DevBoxInstance.machine_generation == box.machine_generation,
            )
        )
        if (
            instance is None
            or not instance.provider_instance_id
            or instance.observed_state != "running"
            or not release_image_matches(box, instance, launch_template)
        ):
            await db.rollback()
            return None
        device = await db.scalar(
            select(DevBoxDevice)
            .where(
                DevBoxDevice.box_id == box.id,
                DevBoxDevice.machine_generation == box.machine_generation,
                DevBoxDevice.instance_id == instance.id,
                DevBoxDevice.enrollment_consumed_at.is_not(None),
                DevBoxDevice.revoked_at.is_(None),
            )
            .order_by(DevBoxDevice.enrollment_consumed_at.desc())
            .limit(1)
        )
        now = datetime.now(UTC)
        if (
            device is None
            or device.user_id != box.user_id
            or device.org_id != box.org_id
            or await current_host(
                db,
                box_id=box.id,
                generation=box.machine_generation,
                provider_instance_id=instance.provider_instance_id,
                user_id=box.user_id,
                org_id=box.org_id,
                deployment_id=box.deployment_id,
            )
            is None
            or not await retire_expired_host_session(db, store, box, now)
        ):
            await db.rollback()
            return None
        live = await db.scalar(
            select(DevBoxAccessSession.id).where(
                DevBoxAccessSession.box_id == box.id,
                DevBoxAccessSession.machine_generation == box.machine_generation,
                DevBoxAccessSession.terminated_at.is_(None),
            )
        )
        if live is not None:
            await db.rollback()
            return None
        try:
            observed = await asyncio.to_thread(
                owned_instance,
                ec2,
                instance.provider_instance_id,
                ownership_tags(box, box),
            )
        except ValueError:
            await db.rollback()
            return None
        if observed.get("State", {}).get("Name") != "running" or (
            launch_template
            and launch_template.image_id
            and observed.get("ImageId") != expected_image_id(instance, launch_template)
        ):
            await db.rollback()
            return None
        provisioning = bool(launch_template and launch_template.profile_revision)
        if provisioning:
            await resume_host_executor(db, box)
        expiry = host_binding_expiry(box, now)
        run_id = await asyncio.to_thread(
            store.mint,
            tenant_id=box.org_id,
            user_id=box.user_id,
            session_id=box.id,
            lease_generation=box.machine_generation,
            lease_expires_at=int(expiry.timestamp()),
            target_kind="ec2-instance",
            target_ref=instance.provider_instance_id,
            deployment_id=box.deployment_id,
            provisioning=provisioning,
            now=int(now.timestamp()),
        )
        box.auth_readiness = "pending" if provisioning else "ready"
        instance.readiness_class = None
        db.add(
            DevBoxAccessSession(
                box_id=box.id,
                user_id=box.user_id,
                org_id=box.org_id,
                deployment_id=box.deployment_id,
                machine_generation=box.machine_generation,
                device_id=device.id,
                provider_session_ref=run_id,
                expires_at=expiry,
            )
        )
        try:
            await db.commit()
        except Exception:
            await db.rollback()
            await asyncio.to_thread(
                store.revoke_for_lease,
                tenant_id=box.org_id,
                session_id=box.id,
                lease_generation=box.machine_generation,
                expected_run_id=run_id,
            )
            raise
        logger.info(
            "dev-box host run re-bound after start: box=%s instance=%s generation=%s expires=%s",
            box.id,
            instance.provider_instance_id,
            box.machine_generation,
            expiry.isoformat(),
        )
        return run_id


async def reconcile_host_readiness(session_factory, ec2, ssm, store, instance_id, *, launch_template):
    if (not launch_template or not launch_template.profile_revision or not launch_template.source_revision
            or not launch_template.gateway_execute_arn or not launch_template.crew_model_id):
        return False
    async with session_factory() as db:
        instance = await db.get(DevBoxInstance, instance_id)
        box = await db.get(DevBox, instance.box_id) if instance else None
        if (box is None or box.profile != "crew" or box.auth_readiness != "pending"
                or not instance.certificate_fingerprint
                or not release_image_matches(box, instance, launch_template)
                or not await ec2_crew_allowed(db, box.org_id)):
            return False
        # Verify against the release THIS generation was provisioned from (#8223 cause 34).
        pins = await resolve_generation_pins(db, ssm, instance, launch_template)
        refusal = dashboard_origin_refusal()
        if refusal is not None:
            # Never configure a loopback dashboard behind the edge (#8223 cause 20): name the
            # refusal on the box's operation and in the log, and retry on the next tick.
            recorded = await record_readiness_refusal(db, box, refusal)
            logger.warning(
                "dev-box readiness refused: box=%s instance=%s reason=%s feature_crew_dashboard=true "
                "CREW_DASHBOARD_ORIGIN=%r event_recorded=%s",
                box.id, instance.provider_instance_id, refusal, os.environ.get("CREW_DASHBOARD_ORIGIN", ""), recorded,
            )
            return False
        access = await db.scalar(select(DevBoxAccessSession).where(
            DevBoxAccessSession.box_id == box.id,
            DevBoxAccessSession.user_id == box.user_id,
            DevBoxAccessSession.org_id == box.org_id,
            DevBoxAccessSession.deployment_id == box.deployment_id,
            DevBoxAccessSession.machine_generation == box.machine_generation,
            DevBoxAccessSession.terminated_at.is_(None),
            DevBoxAccessSession.expires_at > datetime.now(UTC),
        ))
        executor = await db.scalar(select(CrewExecutor).where(
            CrewExecutor.tenant_id == box.org_id, CrewExecutor.user_id == box.user_id,
            CrewExecutor.target_kind == "ec2-instance", CrewExecutor.dev_box_id == box.id,
            CrewExecutor.generation == box.machine_generation,
            CrewExecutor.state == "provisioning",
        ))
        checkpoint = await db.get(CrewCheckpoint, executor.latest_verified_checkpoint_id) if executor and executor.latest_verified_checkpoint_id else None
        device = await db.get(DevBoxDevice, access.device_id) if access else None
        # A first host has no verified checkpoint to restore (#8171): it is verified with a
        # fresh scope (restore_manifest_version 0, no checkpoint pins) and starts with an
        # empty Crew home. An owner who has one is restored from exactly that checkpoint.
        fresh = executor is not None and executor.latest_verified_checkpoint_id is None
        if (access is None or not access.provider_session_ref or device is None
                or device.revoked_at or device.machine_generation != box.machine_generation
                or not device.enrollment_consumed_at or executor is None
                or (not fresh and (
                    checkpoint is None or checkpoint.status != "verified" or checkpoint.restore_pins
                    or checkpoint.tenant_id != box.org_id or checkpoint.user_id != box.user_id
                    # Not `checkpoint.owner_key != executor.owner_key`: a replaced box gets a NEW
                    # executor row with a fresh owner_key that inherits the predecessor's verified
                    # checkpoint (inventory.bind_host), so that comparison refused readiness forever
                    # once an owner had any checkpoint (dev 2026-10-21 gen 28, #8223 cause 47). The
                    # lineage is the executor's own latest_verified_checkpoint_id plus tenant/user.
                    or not checkpoint.manifest_sha256 or not checkpoint.manifest_version_id))
                or await current_host(db, box_id=box.id, generation=box.machine_generation,
                                      provider_instance_id=instance.provider_instance_id, user_id=box.user_id,
                                      org_id=box.org_id, deployment_id=box.deployment_id) is None):
            return False
        now = datetime.now(UTC)
        run = await asyncio.to_thread(store.read_run, access.provider_session_ref)
        if (run is None or run.get("status") != "provisioning"
                or run.get("tenant_id") != box.org_id or run.get("user_id") != box.user_id
                or run.get("session_id") != box.id or run.get("lease_generation") != box.machine_generation
                or run.get("target_ref") != instance.provider_instance_id
                or run.get("deployment_id") != box.deployment_id
                or int(run.get("lease_expires_at", 0)) <= int(now.timestamp())):
            return False
        try:
            observed = await asyncio.to_thread(owned_instance, ec2, instance.provider_instance_id, ownership_tags(box, box))
        except ValueError:
            return False
        address = private_host_address(observed)
        if (observed.get("State", {}).get("Name") != "running"
                or observed.get("ImageId") != expected_image_id(instance, launch_template)
                or not address or address != instance.private_address):
            return False
        scope = {
            "run_id": access.provider_session_ref, "tenant_id": box.org_id, "user_id": box.user_id,
            "box_id": box.id, "instance_ref": instance.provider_instance_id, "private_ip": address,
            "profile_revision": pins.profile_revision, "lease_generation": box.machine_generation,
            "restore_manifest_version": 0 if fresh else checkpoint.sequence,
            "checkpoint_id": None if fresh else checkpoint.id,
            "manifest_sha256": None if fresh else checkpoint.manifest_sha256,
        }
        public_scope = {key: value for key, value in scope.items() if key != "run_id"}
        public_scope["run_id_sha256"] = hashlib.sha256(scope["run_id"].encode()).hexdigest()
        bootstrap = crew_bootstrap_command(launch_template, dashboard_origin=crew_dashboard_origin())
        # The Crew home is the owner's retained workspace disk; it must be mounted before the
        # bootstrap restores into it. Without a recorded disk the chain runs as before.
        workspace = await db.scalar(select(DevBoxResource.provider_resource_id).where(
            DevBoxResource.box_id == box.id, DevBoxResource.resource_kind == "workspace_disk",
            DevBoxResource.retention_state == "retained",
        ).order_by(DevBoxResource.machine_generation.desc()).limit(1))
        mount = f"{workspace_mount_command(workspace)} && {ssh_host_key_command()} && {owner_home_command()} && " if workspace else ""
        command_text = (
            f"{mount}{host_profile_command(pins)}"
            f" && (test -e /run/adp/crew-ready || {bootstrap})"
            f" && python3 /opt/adp/crew/verify_crew_ready.py --scope {shlex.quote(json.dumps(public_scope))}"
        )
        command = await asyncio.to_thread(
            ssm.send_command, InstanceIds=[instance.provider_instance_id], DocumentName="AWS-RunShellScript",
            Parameters={"commands": [command_text]}, CloudWatchOutputConfig={"CloudWatchOutputEnabled": False},
        )
        try:
            async with asyncio.timeout(240):
                while True:
                    try:
                        result = await asyncio.to_thread(
                            ssm.get_command_invocation, CommandId=command["Command"]["CommandId"],
                            InstanceId=instance.provider_instance_id,
                        )
                    except ClientError as error:
                        if error.response.get("Error", {}).get("Code") != "InvocationDoesNotExist":
                            raise
                    else:
                        if result.get("Status") not in {"Pending", "InProgress", "Delayed"}:
                            break
                    await asyncio.sleep(ATTESTATION_POLL_SECONDS)
        except TimeoutError:
            return False
        if (result.get("Status") != "Success" or result.get("StandardOutputContent") not in
                ("crew-profile-ok\ncrew-ready-ok\n", "crew-profile-ok\ncrew-bootstrap-ok\ncrew-ready-ok\n")):
            # The host names its refusal class on stderr (e.g. "Crew host bootstrap refused:
            # CrewHomeUnknownProvenanceError"); secret-free, bounded, and the only on-controller
            # signal an operator gets without reading the SSM invocation (#8223 follow-up).
            refusal_class = await record_host_refusal(
                db, box, instance, result.get("StandardErrorContent") or "", result.get("Status"))
            logger.warning(
                "dev-box readiness refused: box=%s instance=%s status=%s refusal=%s stdout=%r stderr=%r",
                box.id, instance.provider_instance_id, result.get("Status"), refusal_class,
                (result.get("StandardOutputContent") or "")[-200:], (result.get("StandardErrorContent") or "")[-400:],
            )
            return False
        await db.refresh(box)
        await db.refresh(instance)
        await db.refresh(access)
        await db.refresh(executor)
        if checkpoint is not None:
            await db.refresh(checkpoint)
        await db.refresh(device)
        if (box.auth_readiness != "pending" or instance.readiness_class == "host_fenced"
                or (fresh and executor.latest_verified_checkpoint_id is not None)
                or (not fresh and (
                    checkpoint.status != "verified" or checkpoint.restore_pins
                    or checkpoint.manifest_sha256 != scope["manifest_sha256"]
                    or executor.latest_verified_checkpoint_id != checkpoint.id))
                or executor.state != "provisioning" or executor.generation != box.machine_generation
                or executor.target_kind != "ec2-instance" or executor.dev_box_id != box.id
                or access.provider_session_ref != scope["run_id"]
                or device.revoked_at or device.machine_generation != box.machine_generation
                or access.terminated_at or access.expires_at.replace(tzinfo=access.expires_at.tzinfo or UTC) <= datetime.now(UTC)
                or not await ec2_crew_allowed(db, box.org_id)
                or await current_host(db, box_id=box.id, generation=box.machine_generation,
                                      provider_instance_id=instance.provider_instance_id, user_id=box.user_id,
                                      org_id=box.org_id, deployment_id=box.deployment_id) is None):
            return False
        box.auth_readiness = "ready"
        instance.readiness_class = "crew_ready"
        instance.error_class = None
        await db.commit()
        # #9455: the gateway refuses every host route (403) while the run is still `provisioning`,
        # and until now the only path that activated it was the owner's own target resolution
        # (publish_host_lease behind GET /crew/target or the Crew dashboard). The host cannot reach
        # that path, so a plain start left Kiro Crew and the owner shell dead for 11–14 minutes on
        # dev (2026-11-01 20:44–20:58, 2026-11-02 06:19–06:31) until somebody opened the dashboard.
        # Activate here, immediately after the attestation that proves the host is ready.
        await activate_attested_run(store, box=box, instance=instance, run_id=access.provider_session_ref)
        return True


async def activate_attested_run(store, *, box, instance, run_id) -> bool:
    """Move the attested host run from ``provisioning`` to ``active`` so its own routes are admitted.

    Idempotent: a run another path already activated (the owner opened the dashboard first) is
    reported as success. A refused activation is logged and leaves the lazy owner-side path as the
    fallback; readiness itself is already committed by the caller.
    """
    if store is None or not getattr(store, "host_role_arn", None) or not run_id:
        return False
    now = int(datetime.now(UTC).timestamp())
    try:
        await asyncio.to_thread(
            store.activate_for_host,
            run_id=run_id, tenant_id=box.org_id, user_id=box.user_id, session_id=box.id,
            lease_generation=box.machine_generation, target_ref=instance.provider_instance_id,
            deployment_id=box.deployment_id, now=now,
        )
    except CrewRunConflictError as error:
        current = await asyncio.to_thread(store.read_run, run_id)
        if current is not None and current.get("status") == "active":
            logger.info("dev-box host run already active: box=%s run=%s", box.id, run_id)
            return True
        logger.warning(
            "dev-box host run activation refused after readiness: box=%s instance=%s run=%s generation=%s status=%s reason=%s",
            box.id, instance.provider_instance_id, run_id, box.machine_generation,
            current.get("status") if current else None, error,
        )
        return False
    except (ClientError, BotoCoreError) as error:
        logger.warning("dev-box host run activation failed after readiness: box=%s run=%s error=%s", box.id, run_id, error)
        return False
    logger.info(
        "dev-box host run activated after readiness: box=%s instance=%s run=%s generation=%s",
        box.id, instance.provider_instance_id, run_id, box.machine_generation,
    )
    return True


async def deliver_sealed_enrollment(db, ec2, ssm, box, instance, device, ciphertext, *, launch_template):
    if (not isinstance(ciphertext, bytes) or len(ciphertext) != 384
            or not all(isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest)
                       for digest in (device.public_key_digest, device.enrollment_digest))):
        raise ValueError("invalid sealed host enrollment")
    now = datetime.now(UTC)
    if (launch_template is None or not launch_template.profile_revision
            or not launch_template.source_revision
            or device.box_id != box.id or device.instance_id != instance.id
            or device.machine_generation != box.machine_generation
            or device.user_id != box.user_id or device.org_id != box.org_id
            or device.deployment_id != box.deployment_id
            or device.revoked_at or device.enrollment_consumed_at
            or not device.enrollment_expires_at
            or device.enrollment_expires_at.replace(tzinfo=device.enrollment_expires_at.tzinfo or UTC) <= now
            or not release_image_matches(box, instance, launch_template)
            or await current_host(db, box_id=box.id, generation=box.machine_generation,
                                  provider_instance_id=instance.provider_instance_id, user_id=box.user_id,
                                  org_id=box.org_id, deployment_id=box.deployment_id) is None):
        return False
    try:
        observed = await asyncio.to_thread(owned_instance, ec2, instance.provider_instance_id, ownership_tags(box, box))
    except ValueError:
        return False
    if observed.get("State", {}).get("Name") != "running" or observed.get("ImageId") != expected_image_id(instance, launch_template):
        return False
    encoded = base64.b64encode(ciphertext).decode("ascii")
    commands = [
        f"set -e; test \"$(sha256sum /run/adp/dev-box-public-key | cut -d ' ' -f 1)\" = {shlex.quote(device.public_key_digest)}"
        f" && printf '%s' {shlex.quote(encoded)} | python3 /opt/adp/crew/write_enrollment.py --sealed"
        " && sha256sum /run/adp/dev-box-enrollment /run/adp/dev-box-public-key",
    ]
    command = await asyncio.to_thread(
        ssm.send_command,
        InstanceIds=[instance.provider_instance_id],
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": commands},
        CloudWatchOutputConfig={"CloudWatchOutputEnabled": False},
    )
    try:
        async with asyncio.timeout(ATTESTATION_TIMEOUT_SECONDS):
            while True:
                try:
                    result = await asyncio.to_thread(
                        ssm.get_command_invocation,
                        CommandId=command["Command"]["CommandId"],
                        InstanceId=instance.provider_instance_id,
                    )
                except ClientError as error:
                    if error.response.get("Error", {}).get("Code") != "InvocationDoesNotExist":
                        raise
                else:
                    if result.get("Status") not in {"Pending", "InProgress", "Delayed"}:
                        break
                await asyncio.sleep(ATTESTATION_POLL_SECONDS)
    except TimeoutError:
        return False
    return result.get("Status") == "Success" and result.get("StandardOutputContent", "").splitlines() == [
        f"{device.enrollment_digest}  /run/adp/dev-box-enrollment",
        f"{device.public_key_digest}  /run/adp/dev-box-public-key",
    ]


async def attest_host(ec2, ssm, box, instance, device, *, expected_image_id=None,
                      expected_profile_revision=None, expected_source_revision=None, expected_bundle_sha256=None):
    try:
        observed = await asyncio.to_thread(
            owned_instance,
            ec2,
            instance.provider_instance_id,
            ownership_tags(box, box),
        )
    except ValueError:
        return False
    if (observed.get("State", {}).get("Name") != "running"
            or (expected_image_id is not None and observed.get("ImageId") != expected_image_id)):
        return False
    commands = ["sha256sum /run/adp/dev-box-enrollment /run/adp/dev-box-public-key"]
    if expected_profile_revision is not None:
        if not expected_source_revision:
            return False
        commands.append(crew_profile_command(expected_profile_revision, expected_source_revision,
                                             bundle_sha256=expected_bundle_sha256))
        commands.append(crew_key_command())
    command = await asyncio.to_thread(
        ssm.send_command,
        InstanceIds=[instance.provider_instance_id],
        DocumentName="AWS-RunShellScript",
        Parameters={
            "commands": commands
        },
        CloudWatchOutputConfig={"CloudWatchOutputEnabled": False},
    )
    try:
        async with asyncio.timeout(ATTESTATION_TIMEOUT_SECONDS):
            while True:
                try:
                    result = await asyncio.to_thread(
                        ssm.get_command_invocation,
                        CommandId=command["Command"]["CommandId"],
                        InstanceId=instance.provider_instance_id,
                    )
                except ClientError as exc:
                    if (
                        exc.response.get("Error", {}).get("Code")
                        != "InvocationDoesNotExist"
                    ):
                        raise
                else:
                    if result.get("Status") not in {"Pending", "InProgress", "Delayed"}:
                        break
                await asyncio.sleep(ATTESTATION_POLL_SECONDS)
    except TimeoutError:
        return False
    lines = result.get("StandardOutputContent", "").splitlines()
    expected_lines = [
        f"{device.enrollment_digest}  /run/adp/dev-box-enrollment",
        f"{device.public_key_digest}  /run/adp/dev-box-public-key",
    ]
    if expected_profile_revision is not None:
        expected_lines.extend(("crew-profile-ok", "crew-key-ok"))
    if result.get("Status") != "Success" or lines != expected_lines:
        return False
    return True


def release_image_matches(box, instance, launch_template):
    """The box belongs to this release's template. A generation with recorded pins is judged
    by them (its own image stays valid after the release moves on, #8223 cause 34); an
    unpinned row still has to run the release's image."""
    return (launch_template is None or not launch_template.profile_revision
            or (box.profile == "crew" and box.template_revision == launch_template.revision
                and (bool(getattr(instance, "profile_revision", None)) or instance.image_ref == launch_template.image_id)))


async def attest_release_host(ec2, ssm, box, instance, device, launch_template):
    if launch_template and launch_template.profile_revision:
        pins = generation_pins(instance, launch_template)
        user_data = pins.crew_bootstrap == "user-data"
        if user_data and not pins.crew_bundle_sha256:
            return False
        return await attest_host(ec2, ssm, box, instance, device,
                                 expected_image_id=pins.image_id,
                                 expected_profile_revision=pins.profile_revision,
                                 expected_source_revision=pins.source_revision,
                                 expected_bundle_sha256=pins.crew_bundle_sha256 if user_data else None)
    return await attest_host(ec2, ssm, box, instance, device)


async def retire_expired_host_session(db, store, box, now):
    sessions = list((await db.scalars(
        select(DevBoxAccessSession).where(
            DevBoxAccessSession.box_id == box.id,
            DevBoxAccessSession.machine_generation == box.machine_generation,
            DevBoxAccessSession.terminated_at.is_(None),
        ).with_for_update()
    )).all())
    if not sessions:
        return True
    if len(sessions) != 1:
        return False
    previous = sessions[0]
    expiry = previous.expires_at.replace(tzinfo=previous.expires_at.tzinfo or UTC)
    if (expiry > now or not previous.provider_session_ref
            or previous.user_id != box.user_id or previous.org_id != box.org_id
            or previous.deployment_id != box.deployment_id):
        return False
    row = await asyncio.to_thread(store.read_run, previous.provider_session_ref)
    if row is not None and (
        row.get("kind") != "crew" or row.get("target_kind") != "ec2-instance"
        or row.get("tenant_id") != box.org_id or row.get("user_id") != box.user_id
        or row.get("session_id") != box.id or row.get("lease_generation") != box.machine_generation
        or row.get("deployment_id") != box.deployment_id
        or isinstance(row.get("lease_expires_at"), bool)
        or not isinstance(row.get("lease_expires_at"), (int, Decimal))
        or row["lease_expires_at"] > now.timestamp()
    ):
        return False
    await asyncio.to_thread(
        store.revoke_for_lease,
        tenant_id=box.org_id, session_id=box.id,
        lease_generation=box.machine_generation,
        expected_run_id=previous.provider_session_ref,
    )
    if await asyncio.to_thread(store.read_run, previous.provider_session_ref) is not None:
        return False
    previous.terminated_at = now
    return True


async def reconcile_expired_host_key(session_factory, ec2, ssm, store, instance_id, *, launch_template):
    if (launch_template is None or not launch_template.profile_revision
            or not launch_template.source_revision):
        return False
    async with session_factory() as db:
        instance = await db.get(DevBoxInstance, instance_id)
        box = await db.scalar(select(DevBox).where(DevBox.id == instance.box_id).with_for_update()) if instance else None
        if (box is None or box.profile != "crew" or not instance.enrollment_public_key
                or not await ec2_crew_allowed(db, box.org_id)
                or not release_image_matches(box, instance, launch_template)
                or await current_host(db, box_id=box.id, generation=box.machine_generation,
                                      provider_instance_id=instance.provider_instance_id, user_id=box.user_id,
                                      org_id=box.org_id, deployment_id=box.deployment_id) is None):
            return False
        now = datetime.now(UTC)
        pending = await db.scalar(select(DevBoxDevice.id).where(
            DevBoxDevice.box_id == box.id, DevBoxDevice.machine_generation == box.machine_generation,
            DevBoxDevice.enrollment_consumed_at.is_(None), DevBoxDevice.revoked_at.is_(None),
            DevBoxDevice.enrollment_expires_at > now,
        ))
        if pending:
            return False
        active = await db.scalar(select(DevBoxAccessSession).where(
            DevBoxAccessSession.box_id == box.id,
            DevBoxAccessSession.machine_generation == box.machine_generation,
            DevBoxAccessSession.terminated_at.is_(None),
        ))
        if active is None and box.auth_readiness != "fenced":
            return False
        if active is not None and active.expires_at.replace(tzinfo=active.expires_at.tzinfo or UTC) > now:
            return False
        try:
            observed = await asyncio.to_thread(owned_instance, ec2, instance.provider_instance_id, ownership_tags(box, box))
        except ValueError:
            return False
        if observed.get("State", {}).get("Name") != "running" or observed.get("ImageId") != expected_image_id(instance, launch_template):
            return False
        old_public = instance.enrollment_public_key
        if active is not None:
            if not await retire_expired_host_session(db, store, box, now):
                return False
            device = await db.get(DevBoxDevice, active.device_id)
            if device is not None:
                device.revoked_at = now
                device.sealed_enrollment = None
            box.auth_readiness = "fenced"
            instance.readiness_class = None
            await db.commit()
        new_public = await rotate_host_key(ssm, instance, old_public, launch_template=launch_template)
        if new_public is None:
            return False
        await db.refresh(box)
        await db.refresh(instance)
        if (instance.enrollment_public_key != old_public
                or not await ec2_crew_allowed(db, box.org_id)
                or await current_host(db, box_id=box.id, generation=box.machine_generation,
                                      provider_instance_id=instance.provider_instance_id, user_id=box.user_id,
                                      org_id=box.org_id, deployment_id=box.deployment_id) is None):
            return False
        instance.enrollment_public_key = new_public
        box.auth_readiness = "pending"
        await db.commit()
        return True


async def rotate_host_key(ssm, instance, old_public, *, launch_template):
    """Retire ``old_public`` on the host and read back its replacement, or ``None`` when the host did not prove one.

    The host refuses to rotate while its run is ready, so the command first clears the readiness receipt
    and stops the host service; ``write_enrollment.py --rotate-key`` is idempotent for the same retired digest.
    """
    command_text = (
        f"set -e; {host_profile_command(generation_pins(instance, launch_template))}"
        " && rm -f /run/adp/crew-ready && systemctl stop adp-crew-host.service"
        " && python3 /opt/adp/crew/write_enrollment.py --rotate-key "
        f"{hashlib.sha256(old_public).hexdigest()}"
        f" && {crew_key_command()}"
    )
    command = await asyncio.to_thread(
        ssm.send_command, InstanceIds=[instance.provider_instance_id], DocumentName="AWS-RunShellScript",
        Parameters={"commands": [command_text]}, CloudWatchOutputConfig={"CloudWatchOutputEnabled": False},
    )
    try:
        async with asyncio.timeout(ATTESTATION_TIMEOUT_SECONDS):
            while True:
                try:
                    result = await asyncio.to_thread(
                        ssm.get_command_invocation, CommandId=command["Command"]["CommandId"],
                        InstanceId=instance.provider_instance_id,
                    )
                except ClientError as error:
                    if error.response.get("Error", {}).get("Code") != "InvocationDoesNotExist":
                        raise
                else:
                    if result.get("Status") not in {"Pending", "InProgress", "Delayed"}:
                        break
                await asyncio.sleep(ATTESTATION_POLL_SECONDS)
    except TimeoutError:
        return None
    output = result.get("StandardOutputContent", "")
    if (result.get("Status") != "Success" or not output.startswith("crew-profile-ok\n")
            or not output.endswith("crew-key-ok\n")):
        return None
    new_public = output[len("crew-profile-ok\n"):-len("crew-key-ok\n")]
    if (not 512 <= len(new_public) <= 2048 or not new_public.startswith("-----BEGIN PUBLIC KEY-----\n")
            or not new_public.endswith("-----END PUBLIC KEY-----\n")
            or new_public.encode("ascii") == old_public):
        return None
    return new_public.encode("ascii")


async def reconcile_expired_enrollment(session_factory, ec2, ssm, device_id, *, launch_template):
    """Rotate a host key that an expired, never-consumed enrollment has burned.

    The owner's approval (POST host-enrollment) binds a device row to the digest of the host's current
    key and refuses any later approval that reuses a digest already on file. When that approval lapses
    before the controller consumes it (controller outage, owner never sealed it), the current key can
    never be approved again, yet nothing fenced the host and no run exists, so the expired-run rotation
    never fires (dev, 2026-10-18: every re-click answered "Host enrollment requires a new public key").
    Rotating here lets the next approval succeed; rows for retired keys or generations are only closed.
    """
    if (launch_template is None or not launch_template.profile_revision
            or not launch_template.source_revision):
        return False
    async with session_factory() as db:
        device = await db.get(DevBoxDevice, device_id, with_for_update=True)
        if device is None or device.enrollment_consumed_at or device.revoked_at:
            return False
        now = datetime.now(UTC)
        expiry = device.enrollment_expires_at
        if not expiry or expiry.replace(tzinfo=expiry.tzinfo or UTC) > now:
            return False
        box = await db.scalar(select(DevBox).where(DevBox.id == device.box_id).with_for_update())
        instance = await db.get(DevBoxInstance, device.instance_id) if device.instance_id else None
        if box is None or box.profile != "crew" or box.status != "running" or box.desired_state != "running":
            return False
        if (instance is None or instance.box_id != box.id
                or device.machine_generation != box.machine_generation
                or instance.machine_generation != box.machine_generation
                or not instance.enrollment_public_key
                or hashlib.sha256(instance.enrollment_public_key).hexdigest() != device.public_key_digest):
            # A retired generation or an already-rotated key: the row can never be approved or consumed.
            device.revoked_at = now
            device.sealed_enrollment = None
            await db.commit()
            return False
        pending = await db.scalar(select(DevBoxDevice.id).where(
            DevBoxDevice.box_id == box.id, DevBoxDevice.machine_generation == box.machine_generation,
            DevBoxDevice.enrollment_consumed_at.is_(None), DevBoxDevice.revoked_at.is_(None),
            DevBoxDevice.enrollment_expires_at > now,
        ))
        active = await db.scalar(select(DevBoxAccessSession.id).where(
            DevBoxAccessSession.box_id == box.id,
            DevBoxAccessSession.machine_generation == box.machine_generation,
            DevBoxAccessSession.terminated_at.is_(None),
        ))
        # A live approval or run owns this key; the expired-run path (reconcile_expired_host_key) rotates fenced hosts.
        if pending or active or box.auth_readiness == "fenced":
            return False
        if (not await ec2_crew_allowed(db, box.org_id)
                or not release_image_matches(box, instance, launch_template)
                or await current_host(db, box_id=box.id, generation=box.machine_generation,
                                      provider_instance_id=instance.provider_instance_id, user_id=box.user_id,
                                      org_id=box.org_id, deployment_id=box.deployment_id) is None):
            return False
        try:
            observed = await asyncio.to_thread(owned_instance, ec2, instance.provider_instance_id, ownership_tags(box, box))
        except ValueError:
            return False
        if observed.get("State", {}).get("Name") != "running" or observed.get("ImageId") != expected_image_id(instance, launch_template):
            return False
        old_public = instance.enrollment_public_key
        new_public = await rotate_host_key(ssm, instance, old_public, launch_template=launch_template)
        if new_public is None:
            return False
        await db.refresh(box)
        await db.refresh(instance)
        await db.refresh(device)
        if (instance.enrollment_public_key != old_public or device.enrollment_consumed_at or device.revoked_at
                or not await ec2_crew_allowed(db, box.org_id)
                or await current_host(db, box_id=box.id, generation=box.machine_generation,
                                      provider_instance_id=instance.provider_instance_id, user_id=box.user_id,
                                      org_id=box.org_id, deployment_id=box.deployment_id) is None):
            return False
        instance.enrollment_public_key = new_public
        device.revoked_at = now
        device.sealed_enrollment = None
        await db.commit()
        return True


async def reconcile_revoked_host_ingress(session_factory, ec2, ssm, store, instance_id, *, launch_template):
    if not launch_template or not launch_template.profile_revision or not launch_template.source_revision:
        return False
    async with session_factory() as db:
        instance = await db.get(DevBoxInstance, instance_id)
        box = await db.scalar(select(DevBox).where(DevBox.id == instance.box_id).with_for_update()) if instance else None
        if (box is None or box.profile != "crew" or box.status != "running" or box.desired_state != "running"
                or box.auth_readiness != "fenced" or instance.readiness_class == "host_fenced"
                or instance.machine_generation != box.machine_generation
                or not instance.provider_instance_id or not release_image_matches(box, instance, launch_template)):
            return False
        policy = await db.scalar(select(DevBoxPolicy.enabled).where(
            DevBoxPolicy.org_id == box.org_id, DevBoxPolicy.deployment_id == box.deployment_id,
        ))
        revoked = await db.scalar(select(TenantMembership.id).where(
            TenantMembership.tenant_id == box.org_id, TenantMembership.user_id == box.user_id,
            TenantMembership.revoked_at.is_not(None),
        ))
        if policy and revoked is None and await ec2_crew_allowed(db, box.org_id):
            return False
        active = await db.scalar(select(DevBoxAccessSession.id).where(
            DevBoxAccessSession.box_id == box.id, DevBoxAccessSession.machine_generation == box.machine_generation,
            DevBoxAccessSession.terminated_at.is_(None),
        ))
        session = await db.scalar(select(DevBoxAccessSession).where(
            DevBoxAccessSession.box_id == box.id, DevBoxAccessSession.machine_generation == box.machine_generation,
            DevBoxAccessSession.terminated_at.is_not(None),
        ).order_by(DevBoxAccessSession.terminated_at.desc()).limit(1))
        if active or session is None or not session.provider_session_ref:
            return False
        device = await db.get(DevBoxDevice, session.device_id)
        if device is None or device.revoked_at is None:
            return False
        try:
            if await asyncio.to_thread(store.read_run, session.provider_session_ref) is not None:
                return False
            observed = await asyncio.to_thread(
                owned_instance, ec2, instance.provider_instance_id, ownership_tags(box, box),
            )
        except (ValueError, OSError, BotoCoreError, ClientError, RuntimeError):
            return False
        if observed.get("State", {}).get("Name") != "running" or observed.get("ImageId") != expected_image_id(instance, launch_template):
            return False
        run_digest = hashlib.sha256(session.provider_session_ref.encode()).hexdigest()
        check = (
            "import hashlib,json,os,stat,sys; "
            "path='/run/adp/crew-host.json'; "
            "descriptor=os.open(path,os.O_RDONLY|os.O_NOFOLLOW); "
            "observed=os.fstat(descriptor); "
            "safe=stat.S_ISREG(observed.st_mode) and observed.st_uid==0 and not observed.st_mode&0o022; "
            "safe or sys.exit(1); "
            "authority=json.load(os.fdopen(descriptor)); "
            "sys.exit(hashlib.sha256(authority['run_id'].encode()).hexdigest()!=sys.argv[1] "
            "or authority['lease_generation']!=int(sys.argv[2]))"
        )
        command_text = (
            f"set -e; {host_profile_command(generation_pins(instance, launch_template))}"
            " && if test -f /run/adp/crew-host.json; then "
            f"python3 -c {shlex.quote(check)} {run_digest} {box.machine_generation}"
            " && rm -f /run/adp/crew-ready && systemctl stop adp-crew-host.service; "
            "else test ! -e /run/adp/crew-ready && ! systemctl is-active --quiet adp-crew-host.service; fi"
        )
        try:
            command = await asyncio.to_thread(
                ssm.send_command, InstanceIds=[instance.provider_instance_id], DocumentName="AWS-RunShellScript",
                Parameters={"commands": [command_text]}, CloudWatchOutputConfig={"CloudWatchOutputEnabled": False},
            )
            async with asyncio.timeout(ATTESTATION_TIMEOUT_SECONDS):
                while True:
                    try:
                        result = await asyncio.to_thread(
                            ssm.get_command_invocation, CommandId=command["Command"]["CommandId"],
                            InstanceId=instance.provider_instance_id,
                        )
                    except ClientError as error:
                        if error.response.get("Error", {}).get("Code") != "InvocationDoesNotExist":
                            raise
                    else:
                        if result.get("Status") not in {"Pending", "InProgress", "Delayed"}:
                            break
                    await asyncio.sleep(ATTESTATION_POLL_SECONDS)
        except (TimeoutError, OSError, BotoCoreError, ClientError):
            return False
        if result.get("Status") != "Success":
            return False
        await db.refresh(box)
        await db.refresh(instance)
        if (box.auth_readiness != "fenced" or box.machine_generation != instance.machine_generation
                or instance.provider_instance_id != observed["InstanceId"]):
            return False
        instance.readiness_class = "host_fenced"
        await db.commit()
        return True


async def reconcile_enrollment(session_factory, ec2, ssm, store, device_id, *, launch_template=None):
    async with session_factory() as db:
        pending = await db.get(DevBoxDevice, device_id)
        if pending is None:
            return None
        box = await db.scalar(
            select(DevBox).where(DevBox.id == pending.box_id).with_for_update()
        )
        if box is None or not await ec2_crew_allowed(db, box.org_id):
            return None
        device = await db.scalar(
            select(DevBoxDevice)
            .where(DevBoxDevice.id == device_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            device is None
            or device.box_id != box.id
            or device.enrollment_consumed_at
            or device.revoked_at
        ):
            return None
        now = datetime.now(UTC)
        expiry = device.enrollment_expires_at
        if (
            not expiry
            or expiry.replace(tzinfo=expiry.tzinfo or UTC) <= now
            or not device.enrollment_digest
        ):
            return None
        recorded = await db.get(DevBoxInstance, device.instance_id)
        if recorded is None or not recorded.provider_instance_id:
            return None
        instance = await current_host(
            db,
            box_id=box.id,
            generation=device.machine_generation,
            provider_instance_id=recorded.provider_instance_id,
            user_id=device.user_id,
            org_id=device.org_id,
            deployment_id=device.deployment_id,
        )
        if (instance is None or instance.id != device.instance_id
                or not release_image_matches(box, instance, launch_template)):
            return None
        if launch_template and launch_template.profile_revision:
            if not device.sealed_enrollment or not await deliver_sealed_enrollment(
                    db, ec2, ssm, box, instance, device, device.sealed_enrollment, launch_template=launch_template):
                return None
        if not await attest_release_host(ec2, ssm, box, instance, device, launch_template):
            return None
        await db.refresh(device)
        now = datetime.now(UTC)
        expiry = device.enrollment_expires_at
        if (
            device.revoked_at
            or device.enrollment_consumed_at
            or not expiry
            or expiry.replace(tzinfo=expiry.tzinfo or UTC) <= now
            or await current_host(
                db,
                box_id=box.id,
                generation=device.machine_generation,
                provider_instance_id=recorded.provider_instance_id,
                user_id=device.user_id,
                org_id=device.org_id,
                deployment_id=device.deployment_id,
            )
            is None
        ):
            return None
        if not await retire_expired_host_session(db, store, box, now):
            return None
        if not await ec2_crew_allowed(db, box.org_id):
            return None
        if launch_template and launch_template.profile_revision:
            # Same generation, new run: a parked (stopped) or stale (ready, lease long expired)
            # executor is returned to provisioning so the readiness command runs for this run.
            await resume_host_executor(db, box)
        expiry = host_binding_expiry(box, now)
        lease_expiry = int(expiry.timestamp())
        run_id = await asyncio.to_thread(
            store.mint,
            tenant_id=box.org_id,
            user_id=box.user_id,
            session_id=box.id,
            lease_generation=box.machine_generation,
            lease_expires_at=lease_expiry,
            target_kind="ec2-instance",
            target_ref=instance.provider_instance_id,
            deployment_id=box.deployment_id,
            provisioning=bool(launch_template and launch_template.profile_revision),
            now=int(now.timestamp()),
        )
        device.enrollment_consumed_at = now
        device.sealed_enrollment = None
        box.auth_readiness = "pending" if launch_template and launch_template.profile_revision else "ready"
        instance.readiness_class = None
        db.add(
            DevBoxAccessSession(
                box_id=box.id,
                user_id=box.user_id,
                org_id=box.org_id,
                deployment_id=box.deployment_id,
                machine_generation=box.machine_generation,
                device_id=device.id,
                provider_session_ref=run_id,
                expires_at=expiry,
            )
        )
        try:
            await db.commit()
        except Exception:
            await db.rollback()
            await asyncio.to_thread(
                store.revoke_for_lease,
                tenant_id=box.org_id,
                session_id=box.id,
                lease_generation=box.machine_generation,
                expected_run_id=run_id,
            )
            raise
        return run_id
