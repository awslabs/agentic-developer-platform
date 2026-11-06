"""Copy optional protected configuration into the private release directory.

This only stages input. stage_configuration validates the complete model and
requires its source, image and generations to match the actual publication.
"""

import json
import os
import stat
from pathlib import Path

MAX_BYTES = 8 * 256 * 1024 + 64 * 1024


def unique(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate_observation_field")
        result[name] = value
    return result


def materialize(environment):
    value = environment.get("ADP_TOOLS_ADAPTER_OBSERVATION_JSON", "")
    if not value:
        return None
    if len(value.encode()) > MAX_BYTES:
        raise ValueError("observation_configuration_too_large")
    configuration = json.loads(value, object_pairs_hook=unique)
    if (
        not isinstance(configuration, dict)
        or set(configuration)
        not in (
            {"schema_version", "mode", "release", "observation"},
            {"schema_version", "mode", "source_revision", "observation"},
        )
        or configuration["schema_version"] != "1.0"
        or configuration["mode"] != "observation"
        or ("release" in configuration and not isinstance(configuration["release"], dict))
        or ("source_revision" in configuration and not isinstance(configuration["source_revision"], str))
        or not isinstance(configuration["observation"], dict)
    ):
        raise ValueError("observation_configuration_invalid")
    parent = Path(environment["RUNNER_TEMP"]) / "adp-adapter-stage"
    details = parent.lstat()
    if not stat.S_ISDIR(details.st_mode) or details.st_mode & 0o077 or details.st_uid != os.getuid():
        raise ValueError("private_observation_directory_required")
    path = parent / "observation.json"
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "w") as stream:
        stream.write(json.dumps(configuration, separators=(",", ":")) + "\n")
    with open(environment["GITHUB_ENV"], "a") as stream:
        stream.write(f"ADP_TOOLS_ADAPTER_OBSERVATION_FILE={path}\n")
    return path


if __name__ == "__main__":
    try:
        materialize(os.environ)
    except (OSError, ValueError, KeyError):
        raise SystemExit("Protected observation configuration is unavailable or invalid") from None
