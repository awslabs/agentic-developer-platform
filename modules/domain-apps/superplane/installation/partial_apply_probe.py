"""Inert retained-file inspection; no credential files or provider execution."""


def program(identity, source_operation_id):
    import re

    if any(
        not isinstance(v, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9-]{0,127}", v)
        for v in [*identity, source_operation_id]
    ):
        raise ValueError("explicit original apply and planning identities required")
    return """
import hashlib, importlib.util, json, os, stat
from pathlib import Path
from workspace_provisioning.terraform import source_digest
identity = IDENTITY
source_operation_id = SOURCE
root = Path('/run/state')
assert not root.is_symlink() and root.is_dir()
assert os.statvfs(root).f_flag & os.ST_RDONLY
private = root / ('superplane-worker-' + str(os.getuid()))
assert not private.is_symlink() and private.is_dir()
info = private.stat()
assert info.st_uid == os.getuid() and not info.st_mode & 0o007
assert info.st_gid == 65532 or not info.st_mode & 0o070
def directory(scope):
    key = hashlib.sha256(json.dumps(scope,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
    path = private / key
    info = path.lstat()
    assert stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
    return path
def read(path):
    info = path.lstat()
    assert stat.S_ISREG(info.st_mode) and info.st_nlink == 1
    assert info.st_uid == os.getuid() and 0 < info.st_size <= 64 << 20
    return path.read_bytes()
def sha(path):
    return hashlib.sha256(read(path)).hexdigest()
target = directory(identity)
source = directory([*identity[:2], source_operation_id])
names = ['workspace.tfplan','workspace-plan.json','workspace-inventory.json',
         'workspace-estimate.json','workspace-authorization.proposed.json','workspace-backend.json']
assert not (source/'review').is_symlink()
files = {name:sha(source/'review'/name) for name in names}
failed = list(target.glob('command-failure-*'))
assert len(failed) == 1 and not failed[0].is_symlink()
assert {p.name for p in failed[0].iterdir()} == {'stdout','stderr'}
errors = read(failed[0]/'stderr').decode()
assert 'DescribeAddressesAttribute' in errors and 'UnauthorizedOperation' in errors
assert 'CreateSecurityGroup' in errors and 'Invalid security group description' in errors
applied = {name:sha(target/name) for name in ['applied-inventory.json','applied-estimate.json','workspace-apply-preflight.json']}
applied.update({str(p.relative_to(target)):sha(p) for p in sorted(failed[0].iterdir())})
package = Path(importlib.util.find_spec('workspace_provisioning').origin).parent
sources = {str(p.relative_to(package)):hashlib.sha256(p.read_bytes()).hexdigest()
           for p in sorted(package.rglob('*.py')) if 'tests' not in p.parts}
print(json.dumps({'version':1,'identity':identity,'read_only':True,
    'operation_directory_exists':True,'runtime_sources':sources,
    'partial_apply': {'kind':'partial-apply-state-v1','apply_started':True,
        'apply_files':applied, 'source_operation_id':source_operation_id,
        'source_files':files,'module_sha256':source_digest(source/'module')}}))
""".replace("IDENTITY", repr(identity)).replace("SOURCE", repr(source_operation_id))
