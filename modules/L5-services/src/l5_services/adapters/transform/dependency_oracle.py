"""Parse pinned Python dependency manifests and reject unrelated lock changes."""

import copy
import re
import tomllib
from hashlib import sha256


_DEPENDENCY = re.compile(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)==([A-Za-z0-9][A-Za-z0-9._+!-]*)\s*")
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")


def _name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value):
        raise ValueError("invalid dependency name")
    return re.sub(r"[-_.]+", "-", value).lower()


def _toml(files, path):
    if path not in files or not isinstance(files[path], bytes) or len(files[path]) > 8 * 1024 * 1024:
        raise ValueError("pinned dependency artifact is missing or oversized")
    try:
        return tomllib.loads(files[path].decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError) as error:
        raise ValueError("dependency artifact is not valid TOML") from error


def _manifest(files):
    document = _toml(files, "pyproject.toml")
    project = document.get("project")
    if not isinstance(project, dict) or not isinstance(project.get("dependencies"), list):
        raise ValueError("project dependencies are unavailable")
    groups = {"project": project["dependencies"]}
    optional = project.get("optional-dependencies", {})
    if not isinstance(optional, dict) or any(not isinstance(value, list) for value in optional.values()):
        raise ValueError("optional dependencies are unavailable")
    groups.update({f"optional:{key}": value for key, value in optional.items()})
    dependencies = {}
    for group, entries in groups.items():
        for entry in entries:
            match = _DEPENDENCY.fullmatch(entry) if isinstance(entry, str) else None
            if match is None:
                raise ValueError("dependency manifest requires exact pinned versions")
            name, version = _name(match[1]), match[2]
            if name in dependencies:
                raise ValueError("dependency has conflicting manifest entries")
            dependencies[name] = (group, version)
    return document, dependencies


def _artifacts(package):
    artifacts = []
    if "sdist" in package:
        artifacts.append(package["sdist"])
    if "wheels" in package:
        if not isinstance(package["wheels"], list):
            raise ValueError("lock wheels are not a list")
        artifacts.extend(package["wheels"])
    if (not artifacts or any(not isinstance(artifact, dict)
                             or not isinstance(artifact.get("hash"), str)
                             or _SHA256.fullmatch(artifact["hash"]) is None
                             for artifact in artifacts)):
        raise ValueError("lock package has no verified artifact hashes")
    return sorted(artifact["hash"] for artifact in artifacts)


def _lock(files):
    document = _toml(files, "uv.lock")
    packages = document.get("package")
    if not isinstance(packages, list) or not packages:
        raise ValueError("lock package list is unavailable")
    indexed = {}
    for package in packages:
        if not isinstance(package, dict) or not isinstance(package.get("version"), str):
            raise ValueError("lock package has no version")
        name = _name(package.get("name"))
        if name in indexed:
            raise ValueError("lock package name has multiple resolutions")
        indexed[name] = package
    return {key: value for key, value in document.items() if key != "package"}, indexed


def _unrelated_lock_packages(packages, project, requested, version):
    """Normalize only uv's required mirror of the selected root requirement."""
    remaining = {name: value for name, value in packages.items() if name != requested}
    root_name = project.get("name")
    if not isinstance(root_name, str):
        return remaining
    root = remaining.get(_name(root_name))
    if (root is None or root.get("source") not in ({"virtual": "."}, {"editable": "."})
            or root.get("version") != project.get("version")):
        return remaining
    metadata = root.get("metadata")
    if not isinstance(metadata, dict) or "requires-dist" not in metadata:
        return remaining
    requirements = metadata["requires-dist"]
    if not isinstance(requirements, list) or any(not isinstance(entry, dict) for entry in requirements):
        raise ValueError("root lock requirements are invalid")
    selected = [index for index, entry in enumerate(requirements) if _name(entry.get("name")) == requested]
    if len(selected) != 1 or requirements[selected[0]].get("specifier") != "==" + version:
        raise ValueError("root lock requirement differs from the selected manifest version")
    normalized = copy.deepcopy(root)
    # Name, marker, extras and every other root field still compare exactly.
    # Only this one version mirror changes when uv regenerates a real lock.
    normalized["metadata"]["requires-dist"][selected[0]]["specifier"] = "==<selected-upgrade>"
    return {**remaining, _name(root_name): normalized}


def compare_dependency_upgrade(base_files: dict[str, bytes], candidate_files: dict[str, bytes],
                               target: dict, expected_artifact_hashes: list[str]):
    if (not isinstance(target, dict) or target.get("kind") != "dependency"
            or not isinstance(target.get("version"), str) or not target["version"]
            or not isinstance(expected_artifact_hashes, list) or not expected_artifact_hashes
            or len(set(expected_artifact_hashes)) != len(expected_artifact_hashes)
            or any(not isinstance(item, str) or _SHA256.fullmatch(item) is None
                   for item in expected_artifact_hashes)):
        raise ValueError("requested dependency artifact pin is unavailable")
    requested = _name(target.get("name"))
    base_document, base_manifest = _manifest(base_files)
    candidate_document, candidate_manifest = _manifest(candidate_files)
    if (requested not in base_manifest or requested not in candidate_manifest
            or base_manifest[requested][0] != candidate_manifest[requested][0]
            or base_manifest[requested][1] == target["version"]
            or candidate_manifest[requested][1] != target["version"]
            or {name: value for name, value in base_manifest.items() if name != requested} !=
               {name: value for name, value in candidate_manifest.items() if name != requested}):
        raise ValueError("manifest does not contain the exact requested upgrade")
    base_project = base_document["project"].copy()
    candidate_project = candidate_document["project"].copy()
    for project in (base_project, candidate_project):
        project.pop("dependencies", None)
        project.pop("optional-dependencies", None)
    if (base_project != candidate_project or
            {key: value for key, value in base_document.items() if key != "project"} !=
            {key: value for key, value in candidate_document.items() if key != "project"}):
        raise ValueError("unrelated manifest fields changed")
    base_metadata, base_lock = _lock(base_files)
    candidate_metadata, candidate_lock = _lock(candidate_files)
    if (base_metadata != candidate_metadata or requested not in base_lock
            or requested not in candidate_lock or
            _unrelated_lock_packages(base_lock, base_document["project"], requested, base_manifest[requested][1]) !=
            _unrelated_lock_packages(candidate_lock, candidate_document["project"], requested, target["version"])):
        raise ValueError("unrelated lock package changed")
    old, new = base_lock[requested], candidate_lock[requested]
    if (old["version"] != base_manifest[requested][1]
            or new["version"] != target["version"]
            or old.get("source") != new.get("source")
            or _artifacts(new) != sorted(expected_artifact_hashes)):
        raise ValueError("lock target differs from pinned requested artifact")
    return {"target": {"kind": "dependency", "name": requested, "version": target["version"]},
            "base_version": old["version"], "candidate_version": new["version"],
            "artifact_hashes": _artifacts(new),
            **{f"{stage}_{path.replace('.', '_')}_sha256": sha256(files[path]).hexdigest()
               for stage, files in (("base", base_files), ("candidate", candidate_files))
               for path in ("pyproject.toml", "uv.lock")}}
