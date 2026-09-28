"""Small protobuf/MessagePack backend fixtures; Terraform rendering is mocked separately."""

import json
import os
import sys
import zipfile


def account_prerequisites(account):
    return {
        "account_id": account,
        "autoscaling_role_arn": f"arn:aws:iam::{account}:role/aws-service-role/autoscaling.amazonaws.com/AWSServiceRoleForAutoScaling",
    }


def with_account_cli(command, directory, account):
    """Run the real preparation/apply CLI with only read-only AWS calls stubbed."""
    binary = directory / "aws"
    binary.write_text(
        f"#!{sys.executable}\nimport json,sys\nfrom pathlib import Path\n"
        f"root=Path({str(directory)!r})\n"
        "if sys.argv[1:3]==['iam','get-role'] and (root/'missing-account-role').exists(): sys.exit(1)\n"
        f"account={account!r};arn={account_prerequisites(account)['autoscaling_role_arn']!r}\n"
        "if sys.argv[1:3]==['sts','get-caller-identity']: print(json.dumps({'Account':account,'Arn':(root/'caller-arn').read_text() if (root/'caller-arn').exists() else f'arn:aws:iam::{account}:user/operator'}))\n"
        "elif sys.argv[1:3]==['iam','get-role']:\n"
        "    name=sys.argv[sys.argv.index('--role-name')+1]\n"
        "    print(json.dumps({'Role':{'Arn':arn if name=='AWSServiceRoleForAutoScaling' else f'arn:aws:iam::{account}:role/{name}'}}))\n"
        "else: sys.exit(99)\n"
    )
    binary.chmod(0o700)
    # PATH is changed only in the child, not in the pytest process or operator shell.
    entry = (
        "import os,runpy,sys;"
        f"os.environ['PATH']={str(directory) + os.pathsep!r}+os.environ.get('PATH','');"
        "sys.argv=sys.argv[1:];sys.path.insert(0,os.path.dirname(sys.argv[0]));"
        "runpy.run_path(sys.argv[0],run_name='__main__')"
    )
    return [command[0], "-c", entry, *command[1:]]


def pack(value):
    if value is None:
        return b"\xc0"
    if isinstance(value, bool):
        return b"\xc3" if value else b"\xc2"
    if isinstance(value, str):
        raw = value.encode()
        return b"\xdb" + len(raw).to_bytes(4, "big") + raw
    if isinstance(value, dict):
        return (
            b"\xde"
            + len(value).to_bytes(2, "big")
            + b"".join(pack(k) + pack(v) for k, v in value.items())
        )
    raise TypeError(type(value))


def varint(value):
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def field(number, value):
    return varint((number << 3) | 2) + varint(len(value)) + value


def backend_config(target):
    return {
        "bucket": "test-state",
        "region": "us-east-1",
        "dynamodb_table": "test-locks",
        "encrypt": True,
        "key": f"{target['environment']}/modules/superplane-workspaces/v2/{target['org_id']}/{target['workspace_id']}/terraform.tfstate",
    }


def saved_plan(path, plan, config, *, workspace="default", kind="s3"):
    backend = (
        field(1, kind.encode())
        + field(2, field(1, pack(config)))
        + field(3, workspace.encode())
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("tfplan", field(13, backend))
        archive.writestr("rendering.json", json.dumps(plan))


def initialize(module, config, *, kind="s3"):
    directory = module / ".terraform"
    directory.mkdir(exist_ok=True)
    (directory / "terraform.tfstate").write_text(
        json.dumps({"backend": {"type": kind, "config": config}})
    )
