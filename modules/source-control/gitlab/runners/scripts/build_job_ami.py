"""Prepare and execute one reviewed private Packer build; never claim live readiness."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]


class AmiBuildError(ValueError):
    pass


def require(condition):
    if not condition:
        raise AmiBuildError("private AMI build binding not verified")


def aws(*args):
    result = subprocess.run(["aws", *args, "--output", "json"], capture_output=True, check=False, timeout=45)
    require(result.returncode == 0)
    return json.loads(result.stdout)


def source_inputs(config, images, root=ROOT):
    require(isinstance(config, dict) and isinstance(images, dict))
    fields = {"account_id": r"[0-9]{12}", "region": r"[a-z]{2}(-[a-z]+)+-[0-9]+",
              "vpc_id": r"vpc-[0-9a-f]{8,17}", "subnet_id": r"subnet-[0-9a-f]{8,17}",
              "security_group_id": r"sg-[0-9a-f]{8,17}", "source_ami": r"ami-[0-9a-f]{8,17}",
              "cluster_name": r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,99}",
              "source_revision": r"[0-9a-f]{40}", "operation_id": r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}"}
    for key, pattern in fields.items():
        require(isinstance(config.get(key), str) and re.fullmatch(pattern, config[key]) is not None)
    account, region = config["account_id"], config["region"]
    require(config.get("instance_profile") == "adp-dev-gitlab-runner-image-builder")
    require(isinstance(config.get("expected_role_arn"), str)
            and re.fullmatch(rf"arn:aws:iam::{account}:role/[a-zA-Z0-9+=,.@_/-]+", config["expected_role_arn"]) is not None)
    require(images.get("status") == "images_published" and images.get("local_only") is False
            and images.get("source_revision") == config["source_revision"]
            and images.get("account_id") == account and images.get("region") == region)
    for name in ("job", "helper", "bootstrap"):
        prefix = f"{account}.dkr.ecr.{region}.amazonaws.com/adp-gitlab-runner-{name}"
        reference = images.get("images", {}).get(name)
        require(isinstance(reference, str) and re.fullmatch(
            re.escape(prefix) + ":" + config["source_revision"] + r"@sha256:[0-9a-f]{64}", reference) is not None)
    checks = images.get("runtime_checks", {})
    require(all(checks.get(key) == "pass" for key in
                ("actual_helper_cache_init", "job_volume_writes", "bootstrap_both_uids_and_connector")))
    require(checks.get("uid") == 10001 and checks.get("capabilities") == "none"
            and checks.get("no_new_privileges") is True)
    lock = json.loads((root / "versions.lock.json").read_text())
    verifier_hash = hashlib.sha256((root / "bootstrap/verify_images.py").read_bytes()).hexdigest()
    require(lock.get("image_verifier_sha256") == verifier_hash)
    values = {key: config[key] for key in fields if key != "cluster_name"}
    values.update(instance_profile=config["instance_profile"], job_image=images["images"]["job"],
                  helper_image=images["images"]["helper"], image_verifier_sha256=verifier_hash,
                  bootstrap_sha256=hashlib.sha256((root / "templates/job-user-data.sh.tftpl").read_bytes()).hexdigest())
    values["images_sha256"] = hashlib.sha256((values["job_image"] + "\n" + values["helper_image"] + "\n").encode()).hexdigest()
    return values


def verify_target(config, call=aws):
    """Read-only preflight: same EKS VPC, no public IP, exact temporary builder profile."""
    account, region = config["account_id"], config["region"]
    identity = call("sts", "get-caller-identity")
    role = config["expected_role_arn"].rsplit("/", 1)[-1]
    require(identity.get("Account") == account and isinstance(identity.get("Arn"), str)
            and identity["Arn"].startswith(f"arn:aws:sts::{account}:assumed-role/{role}/"))
    cluster = call("eks", "describe-cluster", "--name", config["cluster_name"], "--region", region)["cluster"]
    require(cluster["resourcesVpcConfig"]["vpcId"] == config["vpc_id"] and cluster.get("status") == "ACTIVE")
    subnet = call("ec2", "describe-subnets", "--subnet-ids", config["subnet_id"], "--region", region)["Subnets"]
    require(len(subnet) == 1 and subnet[0].get("VpcId") == config["vpc_id"]
            and subnet[0].get("OwnerId") == account and subnet[0].get("MapPublicIpOnLaunch") is False)
    groups = call("ec2", "describe-security-groups", "--group-ids", config["security_group_id"], "--region", region)["SecurityGroups"]
    require(len(groups) == 1 and groups[0].get("VpcId") == config["vpc_id"] and groups[0].get("OwnerId") == account
            and groups[0].get("IpPermissions") == [])
    profile = call("iam", "get-instance-profile", "--instance-profile-name", config["instance_profile"])["InstanceProfile"]
    require(profile.get("Arn") == f"arn:aws:iam::{account}:instance-profile/{config['instance_profile']}"
            and len(profile.get("Roles", [])) == 1
            and profile["Roles"][0].get("Arn") == f"arn:aws:iam::{account}:role/adp-dev-gitlab-runner-image-builder")
    images = call("ec2", "describe-images", "--owners", "137112412989", "--image-ids", config["source_ami"], "--region", region)["Images"]
    require(len(images) == 1 and images[0].get("ImageId") == config["source_ami"]
            and images[0].get("OwnerId") == "137112412989" and images[0].get("Architecture") == "x86_64"
            and images[0].get("State") == "available" and images[0].get("RootDeviceName") == "/dev/xvda"
            and images[0].get("Name", "").startswith("al2023-ami-2023."))
    name = f"adp-gitlab-job-{config['source_revision']}-{config['operation_id']}"
    existing = call("ec2", "describe-images", "--owners", account, "--filters", f"Name=name,Values={name}", "--region", region)
    require(existing.get("Images") == [])
    # A failed Packer attempt can leave a builder even when no AMI exists.
    # Refuse a second launch; only the operator may reconcile owned resources.
    existing = call("ec2", "describe-instances", "--filters",
                    "Name=tag:Module,Values=gitlab-runner-image-builder",
                    f"Name=tag:OperationId,Values={config['operation_id']}",
                    "Name=instance-state-name,Values=pending,running,stopping,stopped,shutting-down",
                    "--region", region)
    require(isinstance(existing.get("Reservations"), list)
            and all(reservation.get("Instances") == [] for reservation in existing["Reservations"]))
    existing = call("ec2", "describe-key-pairs", "--filters",
                    f"Name=key-name,Values=adp-gitlab-image-{config['operation_id']}", "--region", region)
    require(existing.get("KeyPairs") == [])


def verify_published_images(config, images, call=aws):
    """Compare the reviewed receipt with ECR instead of trusting supplied digests."""
    for name in ("bootstrap", "helper", "job"):
        repository = f"adp-gitlab-runner-{name}"
        repos = call("ecr", "describe-repositories", "--registry-id", config["account_id"],
                     "--repository-names", repository, "--region", config["region"])["repositories"]
        require(len(repos) == 1 and repos[0].get("imageTagMutability") == "IMMUTABLE"
                and repos[0].get("registryId") == config["account_id"])
        details = call("ecr", "describe-images", "--registry-id", config["account_id"],
                       "--repository-name", repository, "--image-ids", f"imageTag={config['source_revision']}",
                       "--region", config["region"])["imageDetails"]
        require(len(details) == 1 and config["source_revision"] in details[0].get("imageTags", [])
                and details[0].get("imageDigest") == images["images"][name].split("@", 1)[1])


def retain(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def verify_candidate(config, values, manifest, runtime, call=aws):
    builds = manifest.get("builds", [])
    require(len(builds) == 1)
    artifact = builds[0].get("artifact_id", "")
    require(re.fullmatch(re.escape(config["region"]) + r":ami-[0-9a-f]{8,17}", artifact) is not None)
    image_id = artifact.split(":", 1)[1]
    candidates = call("ec2", "describe-images", "--owners", config["account_id"], "--image-ids", image_id,
                      "--region", config["region"])["Images"]
    require(len(candidates) == 1)
    image = candidates[0]
    require(image.get("ImageId") == image_id and image.get("OwnerId") == config["account_id"]
            and image.get("State") == "available" and image.get("Public") is False and image.get("Architecture") == "x86_64")
    tags = {entry["Key"]: entry["Value"] for entry in image.get("Tags", [])}
    require(tags.get("Module") == "gitlab-runner-image-builder" and tags.get("OperationId") == config["operation_id"]
            and tags.get("SourceRevision") == config["source_revision"])
    for name, key in (("bootstrap_sha256", "bootstrap-sha256"), ("images_sha256", "images-sha256"),
                      ("image_verifier_sha256", "image-verifier-sha256")):
        require(tags.get("adp:gitlab-runner:" + key) == values[name])
    require(runtime.get("image_check_after_docker_restart") is True
            and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", runtime.get("docker_engine_version", "")) is not None)
    require(all(item.get("Ebs", {}).get("Encrypted") is True for item in image.get("BlockDeviceMappings", [])))
    require(bool(image.get("BlockDeviceMappings")))
    return {"image_id": image_id, "owner_id": config["account_id"], "architecture": "x86_64",
            "docker_engine_version": runtime["docker_engine_version"],
            **{key: values[key] for key in ("bootstrap_sha256", "images_sha256", "image_verifier_sha256")}}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--images", type=Path, required=True)
    parser.add_argument("--images-sha256", required=True, help="Reviewed SHA-256 of the published image receipt")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--execute", action="store_true", help="Execute the explicitly authorized saved build configuration")
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    raw_images = args.images.read_bytes()
    require(re.fullmatch(r"[0-9a-f]{64}", args.images_sha256) is not None
            and hashlib.sha256(raw_images).hexdigest() == args.images_sha256)
    images = json.loads(raw_images)
    require(config["source_revision"] == os.environ["ADP_SOURCE_SHA"])
    values = source_inputs(config, images)
    require(not args.output.exists())
    args.output.mkdir(mode=0o700, parents=True)
    values["output_directory"] = str(args.output.resolve())
    variables = args.output / "build.pkrvars.json"
    retain(variables, values)
    receipt = {"source_revision": config["source_revision"], "operation_id": config["operation_id"],
               "images_receipt_sha256": args.images_sha256,
               "status": "preparing", "ami_produced": False, "live_qualified": False,
               "post_snapshot_boot_verified": False}
    receipt_path = args.output / "build-receipt.json"
    retain(receipt_path, receipt)
    try:
        verify_target(config)
        verify_published_images(config, images)
        template = ROOT / "image/job-host.pkr.hcl"
        for operation in ("init", "validate") + (("build",) if args.execute else ()):
            command = ["packer", operation]
            if operation != "init":
                command += ["-var-file=" + str(variables)]
            command.append(str(template))
            with (args.output / f"packer-{operation}.log").open("xb") as log:
                os.chmod(log.name, 0o600)
                result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=False,
                                        timeout=2700 if operation == "build" else 180)
            require(result.returncode == 0)
        if args.execute:
            candidate = verify_candidate(config, values,
                                         json.loads((args.output / "packer-manifest.json").read_text()),
                                         json.loads((args.output / "host-runtime.json").read_text()))
            receipt.update(status="candidate_produced", ami_produced=True, host_ami_candidate=candidate)
        else:
            receipt["status"] = "preflight_passed_no_build"
        retain(receipt_path, receipt)
    except Exception:
        receipt["status"] = "failed_reconcile_owned_builder_and_candidate_before_retry"
        retain(receipt_path, receipt)
        raise
    print(json.dumps({"status": receipt["status"], "ami_produced": receipt["ami_produced"], "live_qualified": False}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit("Private AMI build refused; inspect its retained receipt and owned resources before retry") from None
