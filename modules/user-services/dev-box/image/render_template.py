"""Validate the approved image and template against gateway admission limits."""

import argparse
import json
import re
from pathlib import Path


def render_template(template: dict, policy: dict, image: dict) -> dict:
    required = {"name", "revision", "profile", "instance_size", "disk_gb", "approved"}
    if not required <= template.keys() or template["approved"] is not True:
        raise ValueError("an approved, versioned gateway template is required")
    if template["name"] != "assistant" or template["profile"] != "crew":
        raise ValueError("unsupported template or profile")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", template["revision"]):
        raise ValueError("invalid template revision")
    if template["instance_size"] not in policy["allowed_sizes"]:
        raise ValueError("instance size exceeds admission policy")
    if type(template["disk_gb"]) is not int or not 8 <= template["disk_gb"] <= policy["max_disk_gb"]:
        raise ValueError("disk size exceeds admission policy")
    if set(image) != {"image_id", "image_revision", "source_revision"}:
        raise ValueError("image metadata cannot contain user data or credentials")
    if not re.fullmatch(r"ami-[0-9a-f]{8,17}", image["image_id"]):
        raise ValueError("a pinned image ID is required")
    if not re.fullmatch(r"[0-9a-f]{40}", image["source_revision"]):
        raise ValueError("a full source revision is required")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", image["image_revision"]):
        raise ValueError("invalid image revision")
    if template.get("image_ref") != image["image_id"]:
        raise ValueError("approved template image differs from release image")
    return {
        "name": template["name"],
        "profile": template["profile"],
        "template_revision": template["revision"],
        "image_revision": image["image_revision"],
        "source_revision": image["source_revision"],
        "image_id": image["image_id"],
        "instance_size": template["instance_size"],
        "disk_gb": template["disk_gb"],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    rendered = render_template(
        json.loads(arguments.template.read_text()),
        json.loads(arguments.policy.read_text()),
        json.loads(arguments.image.read_text()),
    )
    arguments.output.write_text(json.dumps(rendered, sort_keys=True, indent=2) + "\n")


if __name__ == "__main__":
    main()
