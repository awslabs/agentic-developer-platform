"""Render an environment-specific Forge artifact outside the public checkout."""

import argparse
import json
import re
import shutil
from pathlib import Path
from urllib.parse import urlsplit


def prepare(configuration, output, *, for_registration=False):
    root = Path(__file__).resolve().parent
    output = Path(output).resolve()
    if output == root or root in output.parents:
        raise ValueError("Use a private build directory outside the source module")
    app = configuration.get("app_id")
    url = configuration["adp_base_url"]
    if for_registration and app is not None:
        raise ValueError("Registration must not replace an existing app identity")
    if not for_registration and (not isinstance(app, str) or not re.fullmatch(r"ari:cloud:ecosystem::app/[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", app)):
        raise ValueError("A registered Forge app ID is required")
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("ADP must be an HTTPS origin without credentials, path, query or fragment")
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    for name in ("package.json", "package-lock.json"):
        shutil.copyfile(root / name, output / name)
    for name in ("static", "tests"):
        shutil.copytree(root / name, output / name, ignore=shutil.ignore_patterns("dist"))
    manifest = (root / "manifest.template.yml").read_text()
    if for_registration:
        manifest = manifest.replace("  id: __FORGE_APP_ID__\n", "")
    else:
        manifest = manifest.replace("__FORGE_APP_ID__", json.dumps(app))
    manifest = manifest.replace("__ADP_BASE_URL__", json.dumps(url.rstrip("/")))
    (output / "manifest.yml").write_text(manifest)
    (output / "manifest.yml").chmod(0o600)
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--configuration", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--for-registration", action="store_true", help="Prepare a new app with no ID for one-time Forge registration")
    args = parser.parse_args()
    prepare(json.loads(args.configuration.read_text()), args.output, for_registration=args.for_registration)
    print("Prepared private Forge build directory.")
