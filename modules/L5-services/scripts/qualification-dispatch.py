#!/usr/bin/env python3
"""Select private release fixtures and require a concrete service driver before native work."""

import argparse
import hashlib
import json
import os
import re
import stat
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = Path(__file__).resolve().parent
SERVICES = {"security", "devops", "finops", "transform", "well_architected"}
NATIVE_CONSOLE_SERVICES = {"finops"}
BROWSER_SERVICES = {"security", "devops", "finops"}
MAX_FIXTURE_BYTES = 262144


def relative_path(value):
    if (not isinstance(value, str) or not value or "\\" in value
            or any(ord(char) < 32 for char in value)
            or PurePosixPath(value).is_absolute()
            or any(part in {"", ".", ".."} for part in value.split("/"))):
        raise ValueError("Portable fixture paths must be relative and contained")
    return value


def private_directory(path, root):
    if path == root:
        return
    private_directory(path.parent, root)
    path.mkdir(mode=0o700, exist_ok=True)
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise ValueError("Portable fixture directory has unsafe permissions")


def resolve_paths(value, directory):
    if isinstance(value, dict):
        return {key: resolve_paths(item, directory) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_paths(item, directory) for item in value]
    if isinstance(value, str) and value.startswith("fixture://"):
        return str(directory / relative_path(value.removeprefix("fixture://")))
    if isinstance(value, str) and value.startswith("checkout://"):
        selected = ROOT / relative_path(value.removeprefix("checkout://"))
        # Use the reviewed checkout's CLI; never transport executable code in
        # the private fixture or select unrelated files from the runner.
        if not selected.resolve(strict=True).is_relative_to(ROOT / "modules/gateway/cli"):
            raise ValueError("Only the reviewed CLI may be selected from the checkout")
        return str(selected.resolve(strict=True))
    return value


def portable_fixture(value, source_sha, directory):
    if "bundle_version" not in value:
        return value, {}, [], {}
    if (type(value["bundle_version"]) is not int or value["bundle_version"] != 1
            or set(value) - {"bundle_version", "source_sha", "fixture", "files", "directories", "environment"}
            or value.get("source_sha") != source_sha or not isinstance(value.get("fixture"), dict)
            or not isinstance(value.get("files", {}), dict)
            or not isinstance(value.get("directories", []), list)
            or not isinstance(value.get("environment", {}), dict)):
        raise ValueError("Invalid source-bound portable qualification bundle")
    files, directories = {}, []
    for name, payload in value.get("files", {}).items():
        name = relative_path(name)
        if name == "fixture.json" or name.startswith("evidence/") or name == "evidence":
            raise ValueError("Portable inputs cannot supply generated evidence")
        if not isinstance(payload, dict) or set(payload) != {"json"}:
            raise ValueError("Portable client files must contain JSON data")
        files[name] = json.dumps(resolve_paths(payload["json"], directory), sort_keys=True).encode()
    for name in value.get("directories", []):
        directories.append(relative_path(name))
    for name in [*files, *directories]:
        if (name.split("/")[0] == "fixture.json"
                or any(str(parent) in files for parent in PurePosixPath(name).parents)):
            raise ValueError("Portable file and directory paths overlap")
    if set(files) & set(directories):
        raise ValueError("Portable file and directory paths overlap")
    environment = value.get("environment", {})
    for name, content in environment.items():
        if (not re.fullmatch(r"ADP_L5_FIXTURE_[A-Z0-9_]+", name) or name in os.environ
                or not isinstance(content, str) or not content
                or any(char in content for char in ("\r", "\n", "\x00"))):
            raise ValueError("Portable sessions cannot replace runner configuration")
    return resolve_paths(value["fixture"], directory), files, directories, environment


def service_name(value):
    service = value.replace("-", "_")
    if service not in SERVICES:
        raise ValueError("Unknown L5 qualification service")
    return service


def check_driver(service, scripts=SCRIPTS):
    service = service_name(service)
    if service == "security":
        names = ("qualify-security-readbacks.py", "emit-security-qualification.py")
    else:
        names = (f"qualify-{service.replace('_', '-')}-release.py",
                 f"emit-{service.replace('_', '-')}-qualification.py")
    for name in names:
        path = scripts / name
        if path.is_symlink() or not path.is_file() or not path.stat().st_mode & stat.S_IXUSR:
            raise ValueError(f"Missing executable native qualification driver: {name}")
    return names


def check_console_policy(service, policy):
    """Gate only drivers whose current native acceptance opens an AWS console.

    This is target policy, supplied by the protected release environment. A
    fixture or workflow-dispatch input cannot enable console automation.
    Offline tests and documented API/CLI qualification remain independent.
    """
    service = service_name(service)
    if service in NATIVE_CONSOLE_SERVICES and policy != "allowed":
        raise ValueError(
            f"native_console_automation_disabled: {service} native qualification "
            "requires AWS console automation, which this target does not allow. "
            "Keep native acceptance unresolved; code, offline tests and API work may continue."
        )


def check_browser_policy(service, policy):
    """Keep first-party UI testing distinct from AWS management-console policy."""
    service = service_name(service)
    if service in BROWSER_SERVICES - {"security", "devops"} and policy != "allowed":
        raise ValueError(
            f"browser_automation_disabled: {service} full qualification includes UI checks. "
            "The protected target does not allow browser automation. Run the separate "
            "headless checks and keep UI acceptance pending; DevOps native access uses "
            "ADP login and SSO into the DevOps UI. Do not enable general AWS console "
            "automation to bypass this boundary."
        )


def materialize(service, source_sha, fixture_json, runner_temp, github_env):
    service = service_name(service)
    if not re.fullmatch(r"[a-f0-9]{40}", source_sha):
        raise ValueError("Select a reviewed source revision")
    content = fixture_json.encode("utf-8")
    if not content or len(content) > MAX_FIXTURE_BYTES:
        raise ValueError("Private qualification fixture is missing or exceeds its bound")
    try:
        fixture = json.loads(content)
    except (ValueError, UnicodeError) as error:
        raise ValueError("Private qualification fixture must be JSON") from error
    if not isinstance(fixture, dict):
        raise TypeError("Private qualification fixture must be a JSON object")
    is_bundle = "bundle_version" in fixture
    base = Path(runner_temp)
    if (not base.is_absolute() or any(char in str(base) for char in ("\r", "\n"))
            or base.resolve(strict=True).is_relative_to(ROOT)):
        raise ValueError("Private fixture directory must be outside the checkout")
    parent = base / "l5-services-qualification"
    directory = parent / service
    fixture, files, directories, environment = portable_fixture(fixture, source_sha, directory)
    references = [fixture.get("source_sha"), fixture.get("release_sha")]
    for field in ("authorization", "release"):
        value = fixture.get(field)
        if isinstance(value, dict):
            references.extend((value.get("source_sha"), value.get("worker") if field == "release" else None))
    pinned = [reference for reference in references if reference is not None]
    if not pinned or any(reference != source_sha for reference in pinned):
        raise ValueError("Private qualification fixture belongs to another source revision")
    parent.mkdir(mode=0o700, exist_ok=True)
    if parent.is_symlink() or parent.stat().st_mode & 0o077 or parent.resolve().is_relative_to(ROOT):
        raise ValueError("Private fixture directory has unsafe permissions")
    directory.mkdir(mode=0o700, exist_ok=True)
    if (directory.is_symlink() or directory.stat().st_mode & 0o077
            or directory.resolve().is_relative_to(ROOT)):
        raise ValueError("Private fixture directory has unsafe permissions")
    evidence = directory / "evidence"
    evidence.mkdir(mode=0o700, exist_ok=True)
    if evidence.is_symlink() or evidence.stat().st_mode & 0o077:
        raise ValueError("Private evidence directory has unsafe permissions")
    for name in directories:
        private_directory(directory / name, directory)
    for name, data in files.items():
        selected = directory / name
        private_directory(selected.parent, directory)
        descriptor = os.open(selected, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
    if is_bundle:
        content = json.dumps(fixture, sort_keys=True).encode()
    path = directory / "fixture.json"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(content)
    with open(github_env, "a", encoding="utf-8") as output:
        if environment:
            os.fchmod(output.fileno(), 0o600)
        output.write(f"PRIVATE_FIXTURE={path}\nPRIVATE_EVIDENCE_DIR={evidence}\n"
                     f"L5_FIXTURE_DIGEST={hashlib.sha256(content).hexdigest()}\n")
        for name, value in environment.items():
            if os.environ.get("GITHUB_ACTIONS") == "true":
                # GitHub masks the individual session, including when it was
                # originally supplied as part of a larger protected JSON secret.
                print("::add-mask::" + value.replace("%", "%25"), flush=True)
            output.write(f"{name}={value}\n")
    return path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("prepare", "check-driver", "check-console-policy", "check-browser-policy"))
    parser.add_argument("service")
    arguments = parser.parse_args()
    if arguments.action == "check-console-policy":
        check_console_policy(arguments.service, os.environ.get("L5_AWS_CONSOLE_AUTOMATION", "disabled"))
    elif arguments.action == "check-browser-policy":
        check_browser_policy(arguments.service, os.environ.get("L5_BROWSER_AUTOMATION", "disabled"))
    elif arguments.action == "check-driver":
        check_driver(arguments.service)
    else:
        materialize(arguments.service, os.environ["SOURCE_SHA"], os.environ.get("PRIVATE_FIXTURE_JSON", ""),
                    os.environ["RUNNER_TEMP"], os.environ["GITHUB_ENV"])


if __name__ == "__main__":
    main()
