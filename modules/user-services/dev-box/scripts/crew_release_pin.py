"""Plan and write the Crew image release pin (#8054).

After scripts/build-dev-box-image.sh has published a Crew AMI, the release is
pinned in two places the stage reads:

  SSM /adp/<env>/dev-box/{image-id, image-revision, profile-revision, crew-model-id}
      image-id / image-revision / profile-revision are what the build published
      and scripts/deploy-dev-box.sh reads into image_id / image_revision /
      crew_profile_revision; crew-model-id is the gateway model the Crew host
      supervisor may use (Terraform manages it from crew_model_id, so the tfvars
      receive the same value and the next apply is a no-op).
  environments/<env>/modules/dev-box.tfvars
      crew_model_id = "<id>" (the only Crew pin kept in the file; the image pins
      come from SSM at run time, as the file header says).

The plan is printed first (values, tfvars diff, GitHub variables for the
ADP-managed path); nothing is written unless --apply is given. Every write is
journaled as a JSON line under .adp-dev-box/<env>/release/.
"""

import argparse
import datetime
import difflib
import json
import re
import subprocess
import sys
from pathlib import Path

SSM_PIN_KEYS = ("image-id", "image-revision", "profile-revision", "crew-model-id")
GITHUB_VARIABLES = {"image-id": "ADP_DEV_BOX_IMAGE_ID", "image-revision": "ADP_DEV_BOX_IMAGE_REVISION",
                    "profile-revision": "ADP_DEV_BOX_CREW_PROFILE_REVISION", "crew-model-id": "ADP_DEV_BOX_CREW_MODEL_ID"}
AMI_PATTERN = re.compile(r"ami-[0-9a-f]{8,17}")
REVISION_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
MODEL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}")
ENVIRONMENT_PATTERN = re.compile(r"[a-z][a-z0-9-]{0,31}")
SOURCE_PATTERN = re.compile(r"[0-9a-f]{40}")
TFVARS_LINE = re.compile(r'^(?P<indent>[ \t]*)(?P<comment>#[ \t]*)?crew_model_id[ \t]*=[ \t]*"[^"]*"[ \t]*(?P<trail>#.*)?$')


def rewrite_tfvars(text: str, crew_model_id: str) -> str:
    """Set crew_model_id in tfvars text: replace the active line, else uncomment the placeholder, else append."""
    lines = text.splitlines(keepends=True)
    active = [index for index, line in enumerate(lines) if TFVARS_LINE.match(line.rstrip("\n")) and not TFVARS_LINE.match(line.rstrip("\n"))["comment"]]
    commented = [index for index, line in enumerate(lines) if TFVARS_LINE.match(line.rstrip("\n")) and TFVARS_LINE.match(line.rstrip("\n"))["comment"]]
    replacement = f'crew_model_id = "{crew_model_id}"'
    if active:
        index = active[0]
        match = TFVARS_LINE.match(lines[index].rstrip("\n"))
        trail = f"   {match['trail']}" if match["trail"] else ""
        lines[index] = f"{match['indent']}{replacement}{trail}\n"
    elif commented:
        index = commented[0]
        match = TFVARS_LINE.match(lines[index].rstrip("\n"))
        lines[index] = f"{match['indent']}{replacement}   # pinned by release-crew-image.sh\n"
    else:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(f"\n# Pinned by modules/user-services/dev-box/scripts/release-crew-image.sh\n{replacement}\n")
    return "".join(lines)


def plan(*, environment: str, image_id: str, image_revision: str, profile_revision: str, crew_model_id: str,
         source_revision: str = "", bundle_sha256: str = "", tfvars_path: Path | None = None) -> dict:
    if not ENVIRONMENT_PATTERN.fullmatch(environment):
        raise ValueError("invalid environment name")
    if not AMI_PATTERN.fullmatch(image_id):
        raise ValueError(f"invalid image id: {image_id!r}")
    if not REVISION_PATTERN.fullmatch(image_revision):
        raise ValueError("invalid image revision")
    if not REVISION_PATTERN.fullmatch(profile_revision):
        raise ValueError("invalid Crew profile revision")
    if not image_revision.endswith(f"-crew-{profile_revision}"):
        raise ValueError(f"image revision {image_revision!r} was not built with Crew profile {profile_revision!r}")
    if not MODEL_PATTERN.fullmatch(crew_model_id):
        raise ValueError("invalid Crew model id")
    if source_revision and not SOURCE_PATTERN.fullmatch(source_revision):
        raise ValueError("source revision must be a full commit")
    if bundle_sha256 and not re.fullmatch(r"[0-9a-f]{64}", bundle_sha256):
        raise ValueError("bundle digest must be a sha256")
    values = {"image-id": image_id, "image-revision": image_revision, "profile-revision": profile_revision,
              "crew-model-id": crew_model_id}
    ssm = {f"/adp/{environment}/dev-box/{key}": values[key] for key in SSM_PIN_KEYS}
    tfvars = {"path": str(tfvars_path) if tfvars_path else "", "present": False, "changed": False, "diff": "", "after": ""}
    if tfvars_path is not None and tfvars_path.is_file():
        before = tfvars_path.read_text()
        after = rewrite_tfvars(before, crew_model_id)
        tfvars.update(present=True, changed=after != before, after=after,
                      diff="".join(difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                                                        fromfile=f"a/{tfvars_path.name}", tofile=f"b/{tfvars_path.name}")))
    return {"environment": environment, "source_revision": source_revision, "bundle_sha256": bundle_sha256,
            "ssm": ssm, "github_variables": {GITHUB_VARIABLES[key]: values[key] for key in SSM_PIN_KEYS}, "tfvars": tfvars}


def render(release: dict) -> str:
    lines = [f"Crew image release pin for {release['environment']}:"]
    for name, value in release["ssm"].items():
        lines.append(f"  {name} = {value}")
    if release["source_revision"]:
        lines.append(f"  source revision {release['source_revision']}")
    if release["bundle_sha256"]:
        lines.append(f"  bundle sha256 {release['bundle_sha256']}")
    tfvars = release["tfvars"]
    if not tfvars["path"]:
        lines.append("  tfvars: none selected")
    elif not tfvars["present"]:
        lines.append(f"  tfvars: {tfvars['path']} not found (ADP-managed path: set the GitHub variables below)")
    elif tfvars["changed"]:
        lines.append(f"  tfvars diff ({tfvars['path']}):")
        lines.extend("    " + line for line in tfvars["diff"].rstrip("\n").splitlines())
    else:
        lines.append(f"  tfvars: {tfvars['path']} already pins crew_model_id")
    lines.append("  GitHub variables (ADP-managed dev-box-deploy.yml): " +
                 " ".join(f"{name}={value}" for name, value in release["github_variables"].items()))
    return "\n".join(lines)


def apply(release: dict, *, region: str, journal_path: Path, run=subprocess.run, now=None) -> dict:
    for name, value in release["ssm"].items():
        run(["aws", "ssm", "put-parameter", "--region", region, "--name", name, "--type", "String", "--overwrite",
             "--value", value], check=True, capture_output=True)
    tfvars = release["tfvars"]
    if tfvars["present"] and tfvars["changed"]:
        path = Path(tfvars["path"])
        pending = path.with_suffix(path.suffix + ".tmp")
        pending.write_text(tfvars["after"])
        pending.replace(path)
    stamp = (now or datetime.datetime.now(datetime.timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    entry = {"timestamp": stamp, "action": "crew-image-release-pin", "environment": release["environment"],
             "region": region, "ssm": release["ssm"], "source_revision": release["source_revision"],
             "bundle_sha256": release["bundle_sha256"], "tfvars": tfvars["path"] if tfvars["present"] else "",
             "tfvars_changed": bool(tfvars["present"] and tfvars["changed"])}
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    with journal_path.open("a") as journal:
        journal.write(json.dumps(entry, sort_keys=True) + "\n")
    return entry


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", required=True)
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--image-id", required=True)
    parser.add_argument("--image-revision", required=True)
    parser.add_argument("--profile-revision", required=True)
    parser.add_argument("--crew-model-id", required=True)
    parser.add_argument("--source-revision", default="")
    parser.add_argument("--bundle-sha256", default="")
    parser.add_argument("--tfvars", type=Path, default=None, help="environments/<env>/modules/dev-box.tfvars")
    parser.add_argument("--journal", type=Path, default=None, help="JSON-lines journal appended on --apply")
    parser.add_argument("--plan-output", type=Path, default=None, help="write the plan as JSON here")
    parser.add_argument("--apply", action="store_true", help="write the SSM pins and the tfvars; default is plan only")
    arguments = parser.parse_args(argv)
    try:
        release = plan(environment=arguments.env, image_id=arguments.image_id, image_revision=arguments.image_revision,
                       profile_revision=arguments.profile_revision, crew_model_id=arguments.crew_model_id,
                       source_revision=arguments.source_revision, bundle_sha256=arguments.bundle_sha256,
                       tfvars_path=arguments.tfvars)
    except ValueError as error:
        print(f"crew_release_pin.py: {error}", file=sys.stderr)
        return 2
    print(render(release))
    if arguments.plan_output is not None:
        arguments.plan_output.parent.mkdir(parents=True, exist_ok=True)
        arguments.plan_output.write_text(json.dumps(release, indent=2, sort_keys=True) + "\n")
    if not arguments.apply:
        print("plan only: nothing written (add --apply to pin the release)")
        return 0
    journal = arguments.journal or Path(f".adp-dev-box/{arguments.env}/release/crew-image-journal.jsonl")
    try:
        entry = apply(release, region=arguments.region, journal_path=journal)
    except subprocess.CalledProcessError as error:
        detail = error.stderr.decode(errors="replace").strip() if error.stderr else ""
        print(f"crew_release_pin.py: {' '.join(error.cmd[:4])} failed: {detail}", file=sys.stderr)
        return 1
    print(f"pinned: {len(entry['ssm'])} SSM parameters written, tfvars {'updated' if entry['tfvars_changed'] else 'unchanged'}, journal {journal}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
