"""Render operator-supplied Agent Mail manifests and apply an immutable image."""

import argparse
import os
import re
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[4]
PUBLISHER = ROOT / "modules/agent-factory/scripts/build-and-push.sh"
FILES = (
    "namespace",
    "serviceaccount",
    "rbac",
    "pvc",
    "configmap",
    "service",
    "deployment",
)
IMAGE = re.compile(
    r"[0-9]{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com/mcp-agent-mail@sha256:[0-9a-f]{64}"
)


def render(directory, image, skip_ingress):
    documents = []
    allowed = {
        "namespace": {"Namespace"},
        "serviceaccount": {"ServiceAccount"},
        "rbac": {"Role", "RoleBinding"},
        "pvc": {"PersistentVolumeClaim"},
        "configmap": {"ConfigMap"},
        "service": {"Service"},
        "deployment": {"Deployment"},
        "ingress": {"Ingress"},
    }
    for name in FILES + (() if skip_ingress else ("ingress",)):
        text = (directory / (name + ".yaml")).read_text()
        if name == "deployment":
            text = text.replace("${AGENT_MAIL_IMAGE}", image)
        if "${" in text:
            raise ValueError(
                "Unresolved manifest substitution; supply fully configured manifests"
            )
        parsed = list(yaml.safe_load_all(text))
        if not parsed:
            raise ValueError("Empty manifest")
        for document in parsed:
            if (
                not isinstance(document, dict)
                or document.get("kind") not in allowed[name]
            ):
                raise ValueError(
                    "Unexpected resource kind; manage authentication Secret separately"
                )
            metadata = document.get("metadata", {})
            if metadata.get("namespace", "agent-mail") != "agent-mail":
                raise ValueError("Manifest namespace must be agent-mail")
            if (
                name in {"namespace", "deployment"}
                and metadata.get("name") != "agent-mail"
            ):
                raise ValueError("Namespace and Deployment must be named agent-mail")
            if name == "deployment":
                containers = document["spec"]["template"]["spec"]["containers"]
                if not containers or any(c.get("image") != image for c in containers):
                    raise ValueError(
                        "deployment.yaml must use image: ${AGENT_MAIL_IMAGE}"
                    )
            documents.append(document)
    return yaml.safe_dump_all(documents, sort_keys=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-ingress", action="store_true")
    parser.add_argument(
        "--manifests-dir", type=Path, default=os.getenv("AGENT_MAIL_MANIFESTS_DIR")
    )
    parser.add_argument("--image", default=os.getenv("AGENT_MAIL_IMAGE"))
    parser.add_argument("--context", default=os.getenv("AGENT_MAIL_KUBE_CONTEXT"))
    args = parser.parse_args()
    if args.manifests_dir is None:
        parser.error("Supply --manifests-dir: Agent Mail manifests are not bundled")
    directory = Path(args.manifests_dir).resolve()
    if not args.build and not args.image:
        parser.error("Supply a digest URI with --image or request --build")
    if args.image and not IMAGE.fullmatch(args.image):
        parser.error("Image must be an ECR mcp-agent-mail SHA256 digest URI")
    if not args.dry_run and not args.context:
        parser.error("Supply --context explicitly before applying manifests")
    # Validate all files before publication or Kubernetes mutations.
    preview_image = args.image or "DIGEST_RESOLVED_AFTER_PUBLICATION"
    preview = render(directory, preview_image, args.skip_ingress)
    if args.dry_run:
        if args.build:
            subprocess.run(["bash", str(PUBLISHER), "--dry-run"], check=True)
            if not args.image:
                print(
                    "Manifests validated; deployment rendering awaits the published digest."
                )
                return
        print(preview, end="")
        return
    kubectl = ["kubectl", "--context", args.context]
    # A missing secret or read error fails before publication/application. This
    # command does not retrieve the token and reruns never generate/replace it.
    subprocess.run(
        kubectl
        + ["get", "secret", "agent-mail-auth", "-n", "agent-mail", "-o", "name"],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    image = args.image
    if args.build:
        image = subprocess.check_output(["bash", str(PUBLISHER)], text=True).strip()
        if not IMAGE.fullmatch(image):
            raise ValueError("Publisher did not return an immutable Agent Mail image")
    manifest = render(directory, image, args.skip_ingress)
    subprocess.run(
        kubectl + ["apply", "-n", "agent-mail", "-f", "-"],
        input=manifest,
        text=True,
        check=True,
    )
    subprocess.run(
        kubectl
        + [
            "rollout",
            "status",
            "deployment/agent-mail",
            "-n",
            "agent-mail",
            "--timeout=180s",
        ],
        check=True,
    )


if __name__ == "__main__":
    main()
