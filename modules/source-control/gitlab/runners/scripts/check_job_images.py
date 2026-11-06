"""Exercise real helper cache-init and job writes before publishing job images."""
import json
import re
import subprocess
import uuid


class ImageCheckError(ValueError):
    pass


def command(args, run, **kwargs):
    result = run(args, capture_output=True, check=False, timeout=60, **kwargs)
    if result.returncode:
        raise ImageCheckError("job/helper runtime qualification failed")
    return result.stdout


def check(job, helper, run=subprocess.run):
    for reference in (job, helper):
        if not isinstance(reference, str) or not re.fullmatch(r"[a-z0-9][a-zA-Z0-9./_:@-]{1,255}", reference):
            raise ImageCheckError("invalid image reference")
        metadata = json.loads(command(["docker", "image", "inspect", reference], run))
        if (len(metadata) != 1 or metadata[0].get("Os") != "linux"
                or metadata[0].get("Architecture") != "amd64"
                or metadata[0].get("Config", {}).get("User") != "10001:10001"):
            raise ImageCheckError("job and helper must default to Linux amd64 UID 10001")
    owner = str(uuid.uuid4())
    volumes = [f"adp-runner-image-check-{owner}-{suffix}" for suffix in ("builds", "cache")]
    created = []
    common = ["docker", "run", "--rm", "--network=none", "--cap-drop=ALL",
              "--security-opt=no-new-privileges:true", "--read-only"]
    try:
        for name in volumes:
            command(["docker", "volume", "create", "--label", f"adp-image-check={owner}", name], run)
            created.append(name)
        command([*common, "--mount", f"type=volume,src={volumes[0]},dst=/gitlab-runner-cache-init",
                 helper, "gitlab-runner-helper", "cache-init", "/gitlab-runner-cache-init"], run)
        probe = """import os,pathlib,ssl,http.client,ipaddress,ctypes
assert os.getuid()==10001
s=pathlib.Path('/proc/self/status').read_text()
assert 'CapEff:\\t0000000000000000' in s
assert 'NoNewPrivs:\\t1' in s
for p in [pathlib.Path('/builds/qualification'),pathlib.Path('/cache/qualification')]:
    p.write_text('ok')
    assert p.read_text()=='ok'
"""
        command([*common, "--mount", f"type=volume,src={volumes[0]},dst=/builds",
                 "--mount", f"type=volume,src={volumes[1]},dst=/cache",
                 job, "python3", "-c", probe], run)
    finally:
        # Never prune shared Docker data. Verify each unique volume before removal.
        for name in created:
            metadata = json.loads(command(["docker", "volume", "inspect", name], run))
            if (len(metadata) != 1 or metadata[0].get("Name") != name
                    or metadata[0].get("Labels", {}).get("adp-image-check") != owner):
                raise ImageCheckError("local test volume ownership changed; cleanup refused")
            command(["docker", "volume", "rm", name], run)
    return {"actual_helper_cache_init": "pass", "job_volume_writes": "pass",
            "uid": 10001, "capabilities": "none", "no_new_privileges": True,
            "network": "none", "live_gitlab_transfers_verified": False}
