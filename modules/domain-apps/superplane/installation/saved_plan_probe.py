"""Construct the inert exact-artifact inspection for saved-plan completion."""

import base64
from pathlib import Path
import zlib


def saved_plan_program(identity):
    source = (
        Path(__file__).resolve().parents[1]
        / "workspace_provisioning/saved_plan_validation.py"
    )
    pricing = source.with_name("pricing_plan_validation.py")
    workspace = source.parents[1] / "infra/workspaces"
    replacements = {
        name: (workspace / name).read_bytes()
        for name in ("scripts/check_workspace_plan.py", "regional-pricing.json")
    }
    program = (
        source.read_text()
        + "\n"
        + pricing.read_text()
        + "\n"
        + """
import importlib.util
from workspace_provisioning.planning_boundary import inspection_documents
from workspace_provisioning.terraform import workspace_source
identity = IDENTITY
root = Path('/run/state')
assert not root.is_symlink() and os.statvfs(root).f_flag & os.ST_RDONLY
private = root / ('superplane-worker-' + str(os.getuid()))
assert not private.is_symlink() and private.is_dir()
info = private.stat()
assert info.st_uid == os.getuid() and not info.st_mode & 0o007
assert info.st_gid == 65532 or not info.st_mode & 0o070
key = hashlib.sha256(json.dumps(identity,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
target = private / key
package = Path(importlib.util.find_spec('workspace_provisioning').origin).parent
pricing_refusal = {p.name for p in (target/'review').iterdir()} == ORIGINAL_FILES
runtime_pins = {name: hashlib.sha256((package/name).read_bytes()).hexdigest() for name in SAVED_PLAN_RUNTIME}
accepted_runtimes = (PRICING_RUNTIME,) if pricing_refusal else (SAVED_PLAN_RUNTIME, PRICING_RUNTIME, COMPLETION_RETRY_RUNTIME)
assert runtime_pins in accepted_runtimes
assert hashlib.sha256((target/'module/scripts/prepare_workspace_plan.py').read_bytes()).hexdigest() == SAVED_PLAN_RECIPE
entries = saved_plan_snapshot(target)
documents = inspection_documents(target)
config = json.loads(documents['module/.terraform/terraform.tfstate'])['backend']['config']
backend = {'bucket':config['bucket'],'region':config['region'],'lock_table':config['dynamodb_table']}
if pricing_refusal:
    verify_pricing_entries(entries, workspace_source(), documents, target=documents['target'], backend=backend)
    observation, _ = reproduce_pricing(target, replacements=REPLACEMENT_BYTES)
else:
    verify_saved_entries(entries, workspace_source(), documents, target=documents['target'], backend=backend)
    observation = inspect_saved_plan(target)
assert saved_plan_snapshot(target) == entries
sources = {str(p.relative_to(package)):hashlib.sha256(p.read_bytes()).hexdigest()
           for p in sorted(package.rglob('*.py')) if 'tests' not in p.parts}
print(json.dumps({'version':1, 'identity':identity, 'read_only':True,
    'private_directory_exists':True, 'operation_directory_exists':True,
    'operation_directory_name':key, 'runtime_sources':sources,
    'retained_entries':entries, 'initialization_documents':documents,
    'saved_plan':observation}, sort_keys=True))
""".replace("IDENTITY", repr(identity)).replace("REPLACEMENT_BYTES", repr(replacements))
    )

    encoded = base64.b64encode(zlib.compress(program.encode())).decode()
    return (
        "import base64,zlib;exec(zlib.decompress(base64.b64decode("
        + repr(encoded)
        + ")))"
    )
