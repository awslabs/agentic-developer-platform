"""Check reviewed release inputs before Terraform is allowed to plan manager or host activation."""

import hashlib
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHA256 = r"[0-9a-f]{64}"
IMAGE = rf"[a-z0-9./_-]+:[a-zA-Z0-9._-]+@sha256:{SHA256}"


class ReleaseError(ValueError):
    pass


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require(condition):
    if not condition:
        raise ReleaseError("reviewed_release_incomplete")


def load_lock(path, sha256):
    if path:
        target = Path(path)
        metadata = target.lstat()
        require(target.is_absolute() and target.is_file() and not target.is_symlink()
                and metadata.st_mode & 0o077 == 0 and metadata.st_size <= 65536
                and re.fullmatch(SHA256, sha256 or "") is not None)
        payload = target.read_bytes()
        require(hashlib.sha256(payload).hexdigest() == sha256)
    else:
        require(not sha256)
        payload = (ROOT / "versions.lock.json").read_bytes()
    return json.loads(payload)


def check_executor_policy(root):
    config = tomllib.loads((root / "templates" / "runner.toml.tftpl").read_text())
    require(set(config) == {"runners"} and len(config["runners"]) == 1)
    runner = config["runners"][0]
    require(set(runner) == {"name", "clone_url", "tls-ca-file", "executor", "limit", "request_concurrency", "debug_trace_disabled",
                            "docker", "cache", "autoscaler"})
    require(runner["clone_url"] == "https://${gitlab_host}/gitlab")
    require(runner["tls-ca-file"] == "/secrets/ca.pem")
    require(runner["executor"] == "docker-autoscaler" and runner["debug_trace_disabled"] is True)
    docker = runner["docker"]
    expected = {
        "image": "${image}", "allowed_images": ["${image}"],
        "helper_image": "${helper_image}",
        "pull_policy": "never", "allowed_pull_policies": ["never"],
        "user": "10001:10001", "allowed_users": ["10001:10001"],
        "disable_entrypoint_overwrite": True, "volumes": ["/builds", "/cache"],
        "extra_hosts": ["${gitlab_host}:${gitlab_ip}", "s3.${aws_region}.amazonaws.com:${cache_ip}"],
        "privileged": False, "services_privileged": False, "services_limit": 0,
        "cap_drop": ["ALL"], "services_cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "services_security_opt": ["no-new-privileges:true"],
        "memory": "4g", "memory_swap": "4g", "cpus": "1",
    }
    require(docker == expected and type(docker["services_limit"]) is int and
            type(docker["privileged"]) is bool and type(docker["services_privileged"]) is bool)
    require(runner["cache"] == {
        "Type": "s3", "Path": "group/${group_id}", "Shared": False,
        "s3": {"BucketName": "${cache_bucket}", "BucketLocation": "${aws_region}",
               "AuthenticationType": "iam", "ServerAddress": "s3.${aws_region}.amazonaws.com",
               "PathStyle": True},
    })
    autoscaler = runner["autoscaler"]
    require(set(autoscaler) == {"plugin", "capacity_per_instance", "max_use_count", "max_instances",
                                "plugin_config", "connector_config", "policy"})
    require(autoscaler["plugin"] == "fleeting-plugin-aws" and
            type(autoscaler["capacity_per_instance"]) is int and autoscaler["capacity_per_instance"] == 1 and
            type(autoscaler["max_use_count"]) is int and autoscaler["max_use_count"] == 1 and
            type(autoscaler["max_instances"]) is int and autoscaler["max_instances"] == 2 and
            autoscaler["plugin_config"] == {"name": "${asg_name}", "region": "${aws_region}"} and
            autoscaler["connector_config"] == {"username": "ec2-user", "use_external_addr": False} and
            autoscaler["policy"] == [{"idle_count": 0, "idle_time": "0s"}])
    require(type(runner["limit"]) is int and runner["limit"] == 2 and
            type(runner["request_concurrency"]) is int and runner["request_concurrency"] == 1)


def check(lock, region, host_ami_id, job_image, root=ROOT):
    chart = lock["chart"]
    require(chart["version"] == "0.94.2" and chart["runner_version"] == "19.5.2")
    require(chart["sha256"] == "6a63ce370db021252f4f6f0414a4ff1fb09f8105d683dd8539ee670263944f21")
    require(digest(root / "assets" / "gitlab-runner-0.94.2.tgz") == chart["sha256"])
    require(lock["server_version_observed_by_operator"] == "19.5.1" and lock["compatibility_reviewed"] is True)
    require(lock["manager_image"] == "gitlab/gitlab-runner:alpine-v19.5.2@sha256:800157105b792ed3f0c9bc0aa6bc6427d66889cafd2c8aae4c4c177194699144")
    require(lock["upstream_helper_image"] == "gitlab/gitlab-runner-helper:x86_64-v19.5.2@sha256:072376a64c654169ef9de96b4b40740c10f1ef1a69f913644f56b25d1d894401")
    plugin = lock["fleeting_plugin"]
    require(plugin["version"] == "v1.3.1" and plugin["source_commit"] == "bf5cbcc9183e164637cb4dae42e64e49365e8ade")
    require(plugin["s3_key"] == "gitlab-runners/upstream/fleeting-plugin-aws-linux-amd64")
    require(plugin["sha256"] == "2090afe6b2a888d1e58990dbf7b731124956d27358ff8b8532180235d9d49ebb")
    require(re.fullmatch(rf"python:3\.12\.[0-9]+-slim@sha256:{SHA256}", lock["bootstrap_python_image"]) is not None)
    require(re.fullmatch(SHA256, lock["bootstrap_requirements_sha256"]) is not None)
    require(digest(root / "bootstrap" / "requirements.lock") == lock["bootstrap_requirements_sha256"])
    for name in ("helper_image", "bootstrap_image", "probe_image", "job_image"):
        require(re.fullmatch(IMAGE, lock[name]) is not None)
    require(job_image == lock["job_image"])
    require(lock["helper_image"] != lock["upstream_helper_image"])
    require(re.fullmatch(SHA256, lock["helper_dockerfile_sha256"]) is not None)
    require(digest(root / "bootstrap" / "nonroot-helper.Dockerfile") == lock["helper_dockerfile_sha256"])
    require((root / "bootstrap" / "nonroot-helper.Dockerfile").read_text() ==
            "FROM " + lock["upstream_helper_image"] + "\nRUN install -d -m 0700 -o 10001 -g 10001 "
            "/builds /cache /gitlab-runner-cache-init\nUSER 10001:10001\n")
    require(re.fullmatch(SHA256, lock["image_verifier_sha256"]) is not None)
    require(digest(root / "bootstrap" / "verify_images.py") == lock["image_verifier_sha256"])
    check_executor_policy(root)
    host = lock["host_ami"].get(region)
    require(isinstance(host, dict))
    require(re.fullmatch(r"ami-[0-9a-f]{8,17}", host["image_id"]) is not None)
    require(host["image_id"] == host_ami_id)
    require(re.fullmatch(r"[0-9]{12}", host["owner_id"]) is not None)
    require(host["architecture"] == "x86_64")
    require(re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", host["docker_engine_version"]) is not None)
    require(re.fullmatch(SHA256, host["bootstrap_sha256"]) is not None)
    require(digest(root / "templates" / "job-user-data.sh.tftpl") == host["bootstrap_sha256"])
    images = (lock["job_image"] + "\n" + lock["helper_image"] + "\n").encode()
    require(host["images_sha256"] == hashlib.sha256(images).hexdigest())
    require(host["image_verifier_sha256"] == lock["image_verifier_sha256"])
    require(lock["manager_guard_sha256"] == digest(root / "scripts" / "prepare_manager.py"))
    require(lock["manager_prestart_sha256"] == digest(root / "templates" / "manager-prestart.sh"))
    require(lock["plugin_installer_sha256"] == digest(root / "bootstrap" / "install_plugin.py"))
    require(re.fullmatch(SHA256, lock["loader_source_sha256"]) is not None)
    require(lock["loader_source_sha256"] == digest(root / "bootstrap" / "load_credentials.py"))
    return host


def verify_ami(host, region, run=subprocess.run):
    result = run(["aws", "ec2", "describe-images", "--region", region,
                  "--owners", host["owner_id"], "--image-ids", host["image_id"],
                  "--output", "json"], capture_output=True, check=False)
    require(result.returncode == 0)
    images = json.loads(result.stdout)["Images"]
    require(len(images) == 1)
    image = images[0]
    require(image["ImageId"] == host["image_id"] and image["OwnerId"] == host["owner_id"])
    require(image["Architecture"] == host["architecture"] and image["State"] == "available" and image["Public"] is False)
    tags = {tag["Key"]: tag["Value"] for tag in image.get("Tags", [])}
    require(tags.get("adp:gitlab-runner:bootstrap-sha256") == host["bootstrap_sha256"])
    require(tags.get("adp:gitlab-runner:images-sha256") == host["images_sha256"])
    require(tags.get("adp:gitlab-runner:image-verifier-sha256") == host["image_verifier_sha256"])


def main():
    try:
        query = json.load(sys.stdin)
        lock = load_lock(query.get("lock_path"), query.get("lock_sha256"))
        host = check(lock, query["region"], query["host_ami_id"], query["job_image"])
        verify_ami(host, query["region"])
        print(json.dumps({"ready": "true"}))
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        print(json.dumps({"ready": "false"}))


if __name__ == "__main__":
    main()
