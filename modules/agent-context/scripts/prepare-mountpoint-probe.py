#!/usr/bin/env python3
"""Render a synthetic CSI probe for operator review. Never apply or call AWS.

The isolated namespace, IRSA roles and prefix are new run-owned resources.
Production PVC/bucket data and driver-level IAM are never modified by this tool.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import re

IMAGE = (
    "879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-dev-agent-context-ingestion"
    "@sha256:0de0c4e65ce0810fd00bd0ed2dbb6fa85311490bc52eea115f27d34bd636368d"
)
MODES = ["allow-other", "uid=10001", "gid=10001", "file-mode=0640", "dir-mode=0750"]
PROBE = r"""
import errno
import json
import os
from pathlib import Path
import stat
import sys

def verify_filesystem(execution_mode, filesystem_type, filesystem_source):
    if execution_mode == 'live-csi':
        assert filesystem_type in ('fuse', 'fuse.mountpoint-s3')
        assert filesystem_source == 'mountpoint-s3'
        return 'live-csi-mount'
    if execution_mode == 'local-posix-fixture':
        assert not filesystem_type.startswith('fuse')
        return 'local-fixture-only'
    raise AssertionError('unknown execution mode')

mode = sys.argv[1]
execution_mode = sys.argv[2]
mount = Path('/probe')
records = [line.split() for line in Path('/proc/self/mountinfo').read_text().splitlines()]
record, = [fields for fields in records if fields[4] == '/probe']
filesystem_type = record[record.index('-') + 1]
filesystem_source = record[record.index('-') + 2]
acceptance_scope = verify_filesystem(execution_mode, filesystem_type, filesystem_source)
readonly_mount = 'ro' in record[5].split(',')
assert readonly_mount == (mode != 'writer')
assert not Path('/var/run/secrets/kubernetes.io/serviceaccount/token').exists()
assert not Path('/var/run/secrets/eks.amazonaws.com/serviceaccount/token').exists()
assert not any(k in os.environ for k in ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY',
    'AWS_SESSION_TOKEN', 'AWS_ROLE_ARN', 'AWS_WEB_IDENTITY_TOKEN_FILE',
    'AWS_CONTAINER_CREDENTIALS_FULL_URI', 'AWS_CONTAINER_CREDENTIALS_RELATIVE_URI',
    'AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE'))
for protected in ('/app/s15-probe-marker', '/etc/s15-probe-marker'):
    try:
        Path(protected).write_text('must refuse')
    except OSError as exc:
        assert exc.errno in (errno.EROFS, errno.EACCES, errno.EPERM)
    else:
        raise AssertionError('protected root was writable')
expected = b'{"schema":1,"kind":"s15-synthetic-mount-probe"}\n'
file = mount / 'roundtrip.json'
if mode == 'writer':
    assert os.getuid() == 10001 and os.getgid() == 10001
    # Full-object write/close followed by full-object truncate/overwrite/close.
    with open(file, 'wb') as out:
        out.write(b'initial disposable synthetic value\n')
    with open(file, 'wb') as out:
        out.write(expected)
    assert file.read_bytes() == expected
    shards = mount / 'zoekt-shards'
    shards.mkdir(exist_ok=True)
    with open(shards / 'reader-fixture.txt', 'wb') as out:
        out.write(expected)
    st = file.stat()
    assert (st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode)) == (10001, 10001, 0o640)
    directory = mount.stat()
    assert stat.S_IMODE(directory.st_mode) == 0o750
elif mode == 'reader':
    assert os.getuid() == 0 and 10001 in {os.getgid(), *os.getgroups()}
    assert file.read_bytes() == expected
    assert (mount / 'zoekt-shards/reader-fixture.txt').read_bytes() == expected
    try:
        with open(file, 'wb') as out:
            out.write(b'must refuse read-only mount')
    except OSError as exc:
        assert exc.errno in (errno.EROFS, errno.EACCES, errno.EPERM)
    else:
        raise AssertionError('reader mount was writable')
elif mode == 'outsider':
    assert os.getuid() == os.getgid() == 2002 and 10001 not in os.getgroups()
    try:
        file.read_bytes()
    except OSError as exc:
        assert exc.errno in (errno.EACCES, errno.EPERM)
    else:
        raise AssertionError('unrelated UID could read group-private mount')
else:
    raise AssertionError('unknown probe mode')
print(json.dumps({'mode': mode, 'uid': os.getuid(), 'gid': os.getgid(),
    'filesystem_type': filesystem_type, 'filesystem_source': filesystem_source,
    'acceptance_scope': acceptance_scope, 'readonly_mount': readonly_mount, 'result': 'pass'}))
"""


def plan(run: str, account: str, bucket: str, oidc: str) -> dict:
    if not re.fullmatch(r"[a-f0-9]{12}", run):
        raise ValueError("run must be 12 lowercase hexadecimal characters")
    if account != "879318057152" or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket):
        raise ValueError("reviewed account and valid bucket required")
    if not re.fullmatch(r"oidc.eks.us-east-1.amazonaws.com/id/[A-Za-z0-9]+", oidc):
        raise ValueError("explicit EKS OIDC issuer without URL scheme required")
    namespace = f"security-s15-probe-{run}"
    prefix = f"security-validation/s15/{run}/"
    labels = {"security.adp.dev/s15-probe": run}
    bucket_arn = f"arn:aws:s3:::{bucket}"
    roles = {}
    accounts = []
    for access in ("writer", "reader"):
        role = f"s15-probe-{run}-{access}"
        sa = f"probe-{access}"
        actions = ["s3:GetObject"]
        if access == "writer":
            actions += ["s3:PutObject", "s3:AbortMultipartUpload"]
        roles[access] = {
            "role_name": role,
            "trust": {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {"Federated": f"arn:aws:iam::{account}:oidc-provider/{oidc}"},
                        "Action": "sts:AssumeRoleWithWebIdentity",
                        "Condition": {
                            "StringEquals": {
                                f"{oidc}:aud": "sts.amazonaws.com",
                                f"{oidc}:sub": f"system:serviceaccount:{namespace}:{sa}",
                            }
                        },
                    }
                ],
            },
            "permissions": {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": ["s3:ListBucket"],
                        "Resource": bucket_arn,
                        "Condition": {"StringLike": {"s3:prefix": [prefix, prefix + "*"]}},
                    },
                    {
                        "Effect": "Allow",
                        "Action": actions,
                        "Resource": bucket_arn + "/" + prefix + "*",
                    },
                ],
            },
        }
        accounts.append(
            {
                "apiVersion": "v1",
                "kind": "ServiceAccount",
                "metadata": {
                    "name": sa,
                    "namespace": namespace,
                    "labels": labels,
                    "annotations": {
                        "eks.amazonaws.com/role-arn": f"arn:aws:iam::{account}:role/{role}"
                    },
                },
                "automountServiceAccountToken": False,
            }
        )
    volume_name = namespace
    pv = {
        "apiVersion": "v1",
        "kind": "PersistentVolume",
        "metadata": {"name": volume_name, "labels": labels},
        "spec": {
            "capacity": {"storage": "1Gi"},
            "accessModes": ["ReadWriteMany"],
            "volumeMode": "Filesystem",
            "persistentVolumeReclaimPolicy": "Retain",
            "storageClassName": "",
            "claimRef": {"namespace": namespace, "name": "probe-data"},
            "mountOptions": ["region=us-east-1", "prefix=" + prefix, "allow-overwrite", *MODES],
            "csi": {
                "driver": "s3.csi.aws.com",
                "volumeHandle": volume_name,
                "volumeAttributes": {
                    "bucketName": bucket,
                    "authenticationSource": "pod",
                    "stsRegion": "us-east-1",
                },
            },
        },
    }
    pvc = {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {"name": "probe-data", "namespace": namespace, "labels": labels},
        "spec": {
            "storageClassName": "",
            "volumeName": volume_name,
            "accessModes": ["ReadWriteMany"],
            "resources": {"requests": {"storage": "1Gi"}},
        },
    }
    setup = [
        {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {
                "name": namespace,
                "labels": {
                    **labels,
                    "pod-security.kubernetes.io/enforce": "baseline",
                    "pod-security.kubernetes.io/enforce-version": "v1.35",
                },
            },
        },
        *accounts,
        pv,
        pvc,
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "probe-code", "namespace": namespace, "labels": labels},
            "data": {"probe.py": PROBE},
        },
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": "probe-deny-network", "namespace": namespace, "labels": labels},
            "spec": {
                "podSelector": {"matchLabels": labels},
                "policyTypes": ["Ingress", "Egress"],
                "ingress": [],
                "egress": [],
            },
        },
    ]
    jobs = {}
    for mode, uid, gid in (("writer", 10001, 10001), ("reader", 0, 10001), ("outsider", 2002, 2002)):
        jobs[mode] = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": "probe-" + mode, "namespace": namespace, "labels": labels},
            "spec": {
                "backoffLimit": 0,
                "activeDeadlineSeconds": 180,
                "template": {
                    "metadata": {
                        "labels": labels,
                        "annotations": {"eks.amazonaws.com/skip-containers": "probe"},
                    },
                    "spec": {
                        "automountServiceAccountToken": False,
                        "serviceAccountName": "probe-writer"
                        if mode == "writer"
                        else "probe-reader",
                        "restartPolicy": "Never",
                        "schedulingGates": [{"name": "security.adp.dev/s15-" + run}],
                        "nodeSelector": {
                            "kubernetes.io/os": "linux",
                            "kubernetes.io/arch": "amd64",
                        },
                        "securityContext": {
                            "runAsUser": uid,
                            "runAsGroup": gid,
                            "supplementalGroups": [],
                            "supplementalGroupsPolicy": "Strict",
                            "seccompProfile": {"type": "RuntimeDefault"},
                        },
                        "containers": [
                            {
                                "name": "probe",
                                "image": IMAGE,
                                "imagePullPolicy": "IfNotPresent",
                                "command": [
                                    "python3",
                                    "-B",
                                    "/probe-code/probe.py",
                                    mode,
                                    "live-csi",
                                ],
                                "securityContext": {
                                    "runAsNonRoot": uid != 0,
                                    "allowPrivilegeEscalation": False,
                                    "readOnlyRootFilesystem": True,
                                    "capabilities": {"drop": ["ALL"]},
                                },
                                "resources": {
                                    "requests": {"cpu": "100m", "memory": "128Mi"},
                                    "limits": {
                                        "cpu": "250m",
                                        "memory": "256Mi",
                                        "ephemeral-storage": "64Mi",
                                    },
                                },
                                "volumeMounts": [
                                    {
                                        "name": "data",
                                        "mountPath": "/probe",
                                        "readOnly": mode != "writer",
                                    },
                                    {"name": "code", "mountPath": "/probe-code", "readOnly": True},
                                    {"name": "tmp", "mountPath": "/tmp"},
                                ],
                                "env": [
                                    {"name": "AWS_EC2_METADATA_DISABLED", "value": "true"},
                                    {"name": "HOME", "value": "/tmp"},
                                ],
                            }
                        ],
                        "volumes": [
                            {
                                "name": "data",
                                "persistentVolumeClaim": {
                                    "claimName": "probe-data",
                                    "readOnly": mode != "writer",
                                },
                            },
                            {"name": "code", "configMap": {"name": "probe-code"}},
                            {"name": "tmp", "emptyDir": {"sizeLimit": "32Mi"}},
                        ],
                    },
                },
            },
        }
    # A separately staged denial probe targets a synthetic sibling prefix that
    # has no allowed IAM resources. No existing tenant object is ever requested.
    denied_pv, denied_pvc = deepcopy(pv), deepcopy(pvc)
    denied_pv["metadata"]["name"] += "-denied"
    denied_pv["spec"]["claimRef"]["name"] = "probe-denied"
    denied_pv["spec"]["csi"]["volumeHandle"] += "-denied"
    denied_prefix = f"security-validation/s15/{run}-denied/"
    denied_pv["spec"]["mountOptions"] = [
        "prefix=" + denied_prefix if option.startswith("prefix=") else option
        for option in denied_pv["spec"]["mountOptions"]
    ]
    denied_pvc["metadata"]["name"] = "probe-denied"
    denied_pvc["spec"]["volumeName"] += "-denied"
    denied_job = deepcopy(jobs["reader"])
    denied_job["metadata"]["name"] = "probe-denied-prefix"
    denied_pod = denied_job["spec"]["template"]["spec"]
    denied_pod["securityContext"]["runAsUser"] = 10001
    denied_pod["containers"][0]["securityContext"]["runAsNonRoot"] = True
    denied_pod["volumes"][0]["persistentVolumeClaim"]["claimName"] = "probe-denied"
    denied_pod["containers"][0]["command"] = [
        "python3",
        "-c",
        "raise SystemExit('ERROR: outside-prefix mount unexpectedly succeeded')",
    ]
    return {
        "run": run,
        "namespace": namespace,
        "prefix": prefix,
        "image": IMAGE,
        "roles": roles,
        "setup": setup,
        "jobs": jobs,
        "denied_prefix_stage": {
            "setup": [denied_pv, denied_pvc],
            "job": denied_job,
            "prefix": denied_prefix,
        },
        "status": "prepared_only",
        "requires": [
            "reviewed role creation",
            "fresh CSI and admission preflight",
            "writer completes before readers",
            "live receipts before production rollout",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--oidc", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result = plan(args.run, args.account, args.bucket, args.oidc)
    # Exclusive local artifact creation only. Does not launch/apply anything.
    with Path(args.output).open("x") as out:
        json.dump(result, out, indent=2)
        out.write("\n")


if __name__ == "__main__":
    main()
